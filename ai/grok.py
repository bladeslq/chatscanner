import json
import re
import asyncio
import logging
import httpx
from openai import AsyncOpenAI
from config import GROK_API_KEY, GROK_BASE_URL, GROK_MODEL
from database.models import Client

logger = logging.getLogger(__name__)

# Kaspersky intercepts TLS on Windows — disable cert verification.
# max_retries=0 — we manage retries ourselves; SDK's built-in 58s retry would
# block the semaphore and cascade timeouts during 429 storms.
_http_client = httpx.AsyncClient(verify=False)
client = AsyncOpenAI(
    api_key=GROK_API_KEY,
    base_url=GROK_BASE_URL,
    http_client=_http_client,
    max_retries=0,
)

# Global semaphore — serialize Groq calls. Free tier is 30 RPM; with parallel
# bursts we'd cross the limit and get 429. One in flight + our backoff keeps
# us well under.
_groq_sem = asyncio.Semaphore(1)


# Global pause flag: when set, all callers skip Groq cheaply (return None)
# until this timestamp passes. Set after exhausting 429 retries.
# Messages received during the pause are dropped — once the API recovers,
# only NEW messages are processed.
#
# Pause grows exponentially on consecutive 429s: 30s → 60s → 120s → … → 300s.
# Resets to base after the next successful request.
_paused_until: float = 0.0
_PAUSE_BASE = 60.0  # Groq's 429 window is typically ~60s on free tier
_PAUSE_MAX = 300.0
_next_pause: float = _PAUSE_BASE


def is_paused() -> bool:
    loop = asyncio.get_event_loop()
    return loop.time() < _paused_until


def _trigger_pause():
    """Activate pause and double the next one (capped at _PAUSE_MAX)."""
    global _paused_until, _next_pause
    loop = asyncio.get_event_loop()
    duration = _next_pause
    _paused_until = loop.time() + duration
    _next_pause = min(_next_pause * 2, _PAUSE_MAX)
    return duration


def _reset_pause():
    """Called after a successful request — restore base pause for next 429."""
    global _next_pause
    _next_pause = _PAUSE_BASE

SYSTEM_PROMPT = """Ты — профессиональный аналитик рынка аренды квартир Казани. Извлекаешь структурированные данные из объявлений в Telegram-чатах риелторов.

Ты работаешь ТОЛЬКО с арендой квартир и комнат. Продажу, дома, коттеджи, таунхаусы, дачи — игнорируй (is_listing: false).

## ТИПЫ ОБЪЕКТОВ (только квартиры и комнаты)

- Студия / Гост / Гостинка → property_type=apartment, rooms=1, euro_format=true
- к/2к, комната в квартире → property_type=room, rooms=null
- 1к → property_type=apartment, rooms=1, euro_format=false
- 2к → property_type=apartment, rooms=2, euro_format=false
- 3к → property_type=apartment, rooms=3, euro_format=false
- Евро1к / Е1к — кухня-гостиная без отдельной спальни → rooms=1, euro_format=true
- Евро2к / Е2к / Евро 2к — большая кухня-гостиная + 1 спальня → rooms=2, euro_format=true
- Евро3к / Е3к — кухня-гостиная + 2 спальни → rooms=3, euro_format=true
- Евро4к / Е4к — кухня-гостиная + 3 спальни → rooms=4, euro_format=true

## ПРАВИЛО ЕВРО-ФОРМАТА ДЛЯ ПОДБОРА
Евро-квартира подходит клиентам, ищущим N и N-1 комнат:
- Евро2к → подходит тем, кто ищет 1к или 2к
- Евро3к → подходит тем, кто ищет 2к или 3к
- Евро4к → подходит тем, кто ищет 3к или 4к
Сохрани это в поле euro_rooms_range: [N-1, N]

## СЛОВАРЬ АББРЕВИАТУР РИЕЛТОРОВ КАЗАНИ

### Об объекте
- Соб / Собственник / без агентов → owner_type=owner (не агент)
- Ключи / ключи у риелтора / есть ключи → has_keys=true (оперативный показ)
- До мая / до лета / на зиму → available_until=указанный срок (квартира сдаётся временно)
- Упак — квартира полностью укомплектована мебелью и техникой → condition=упак
- Простая — обычный ремонт → condition=простая
- Дрова — плохое состояние / деревянная отделка → condition=дрова
- Пуля / Пушка / Бомба — отличное соотношение цена/качество → condition=пуля
- Хрущ / Хрущёвка → building_type=хрущёвка
- Сталинка → building_type=сталинка
- Брежневка / Ленинградка → building_type=брежневка

### Условия оплаты
- 50ку / 50 + ку / 50+коммуналка → price=50000, price_includes_utilities=false (КУ по квитанции)
- 50сч / 50 + сч / 50+счётчики → price=50000, price_includes_utilities=false (только счётчики)
- 50вв / 50 всё включено / 50вкл / 50 ВВ → price=50000, price_includes_utilities=true (КУ платит собственник)
- Если формат не указан → price_includes_utilities=null
- КУ — коммунальные услуги; Сч — счётчики (свет, вода)

### Залог
- Залог 100% → deposit = price (сумма аренды)
- Залог 50% / залог ½ → deposit = price / 2
- Делимый / можно разбить / рассрочка / пополам → deposit_negotiable=true
- Без залога / залога нет → deposit=0

### Комиссия и откат
- Комиссия 50% / 100% и т.п. → commission_percent=число
- Откат / отк / от — часть комиссии агенту-партнёру → kickback_percent=число
- С+ / 50/50 — комиссия делится между риелторами поровну → commission_shared=true.
  ВАЖНО: «50/50» — это про комиссию, это НЕ название ЖК и НЕ район. В complex не записывай.

### Требования к жильцам
- СП — семейная пара
- БД / Бд — без детей
- БЖ / Бж — без животных
- Наши — граждане РФ (русские / татары)
- СНГ — граждане стран СНГ
- Ино — иностранцы (дальнее зарубежье)

## ПРАВИЛА ИЗВЛЕЧЕНИЯ

1. Площадь: "60кв" / "60м" / "60м²" / "60 метров" / "60 кв.м" → area=60
2. Цена: только число в рублях. "35" в контексте аренды → 35000. "35т" / "35к" / "35тыс" → 35000.
3. Залог в %: пересчитай в рубли от основной цены аренды.
4. Этаж: "3/9" → floor=3, floors_total=9. "3 из 9" — то же самое.
5. ЖК: названия пишут без "ЖК" — отдельной строкой или после типа квартиры. Если видишь похожее на название ЖК — запиши в complex.
6. Контакт: извлеки имя + телефон или @ник. Если несколько — все через запятую.
7. Дом/коттедж/таунхаус/дача/участок/продажа → is_listing=false, не извлекай.
8. Если поле не упоминается — верни null.
9. Верни ТОЛЬКО JSON без пояснений и markdown-блоков."""

EXTRACTION_PROMPT = """Проанализируй сообщение из Telegram-чата риелторов Казани.

Если это НЕ объявление об АРЕНДЕ квартиры или комнаты — верни: {"is_listing": false}
Сюда относятся: продажа, дома, коттеджи, таунхаусы, дачи, участки, коммерция, вопросы, обсуждения.

Если это объявление об аренде квартиры или комнаты — верни JSON строго в таком формате (без markdown):
{
  "is_listing": true,
  "property_type": "apartment" | "room",
  "rooms": число или null,
  "euro_format": true | false,
  "euro_rooms_range": [число, число] | null,
  "price": число в рублях или null,
  "price_includes_utilities": true | false | null,
  "deposit": число в рублях или null,
  "deposit_negotiable": true | false | null,
  "area": число м² или null,
  "floor": число или null,
  "floors_total": число или null,
  "district": "район Казани" или null,
  "address": "улица и дом" или null,
  "complex": "название ЖК" или null,
  "building_type": "хрущёвка" | "сталинка" | "брежневка" | "новостройка" | "панель" | null,
  "owner_type": "owner" | "agent" | null,
  "has_keys": true | false | null,
  "condition": "упак" | "простая" | "дрова" | "пуля" | null,
  "available_until": "строка срока" | null,
  "commission_percent": число или null,
  "kickback_percent": число или null,
  "commission_shared": true | false | null,
  "tenant_requirements": "требования строкой" | null,
  "description": "краткое описание: техника, мебель, инфраструктура, особенности",
  "contact": "имя, телефон или @ник" | null
}

Сообщение:
"""


async def _groq_request(messages: list, max_tokens: int, temperature: float = 0.1) -> str | None:
    """Make a Groq API request.

    On 429: short retry, then activate a global pause and skip subsequent
    callers cheaply (returns None) until the pause window passes.
    Messages received during the pause are simply dropped — once the API
    recovers, only NEW messages are processed.
    """
    if is_paused():
        return None

    async with _groq_sem:
        for attempt in range(3):
            try:
                response = await client.chat.completions.create(
                    model=GROK_MODEL,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                _reset_pause()
                return response.choices[0].message.content.strip()
            except Exception as e:
                msg = str(e)
                if "429" in msg:
                    if attempt < 2:
                        await asyncio.sleep(2 ** attempt * 3)  # 3s, 6s
                        continue
                    duration = _trigger_pause()
                    logger.warning(f"Groq 429 — pausing extraction for {duration:.0f}s")
                    return None
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt * 2)
                    continue
                duration = _trigger_pause()
                logger.warning(f"Groq transient error — pausing {duration:.0f}s: {e}")
                return None
    return None


async def extract_listing(message_text: str) -> dict:
    """Extract real estate listing data from a Telegram message."""
    if len(message_text.strip()) < 20:
        return {"is_listing": False}
    content = await _groq_request(
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": EXTRACTION_PROMPT + message_text},
        ],
        max_tokens=900,
    )
    if not content:
        return {"is_listing": False}
    match = re.search(r"\{.*\}", content, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except Exception:
            pass
    return {"is_listing": False}


_CONFLICT_SYSTEM = (
    "Ты определяешь, запрещает ли объявление аренды конкретному клиенту снять квартиру.\n\n"

    "## Аббревиатуры риелторов Казани (требования к жильцам)\n\n"
    "СП — семейная пара\n"
    "БД / Бд — без детей (детям нельзя)\n"
    "БЖ / Бж — без животных / без питомцев (кошки, собаки и др. — нельзя)\n"
    "Наши — только граждане РФ (русские, татары)\n"
    "СНГ — граждане стран СНГ\n"
    "Ино / Иностранцы — иностранцы из дальнего зарубежья\n"
    "Только девушку / только девочку — только женщины-одиночки\n"
    "Только парня / только мужчину — только мужчины-одиночки\n"
    "Только СП / только семейные — только семейные пары\n\n"

    "## Словарь формулировок\n\n"
    "ЗАПРЕЩАЮЩИЕ — означают НЕЛЬЗЯ этому типу жильцов:\n"
    "  'без X', 'не X', 'только без X', 'не рассматриваем X', 'не сдаём X',\n"
    "  'только наши', 'только РФ', 'только СП', 'только девушку', 'только парня'\n\n"
    "РАЗРЕШАЮЩИЕ — означают МОЖНО, это НЕ ограничение:\n"
    "  'можно X', 'X — ок', 'X — можно', 'рассмотрим X', 'X welcome',\n"
    "  'можно ино', 'ино ок', 'можно с детьми', 'можно с животными',\n"
    "  'можно СНГ', 'можно наших', 'рассмотрим семейных'\n\n"

    "## Правила\n\n"
    "КОНФЛИКТ = 'да' — только если объявление ЯВНО ЗАПРЕЩАЕТ профиль клиента:\n"
    "  • БЖ / без животных / без питомцев + у клиента есть животные/питомцы → да\n"
    "  • БД / без детей + у клиента есть дети → да\n"
    "  • только наши / только РФ + клиент иностранец → да\n"
    "  • только девушку + клиент мужчина → да\n"
    "  • только парня + клиент женщина → да\n"
    "  • только СП + клиент одиночка → да\n\n"
    "НЕТ КОНФЛИКТА = 'нет' — во всех остальных случаях:\n"
    "  • 'Можно ино' + клиент иностранец → нет (разрешение, не запрет)\n"
    "  • 'Можно с детьми' + у клиента дети → нет\n"
    "  • 'Ино ок' + клиент иностранец → нет\n"
    "  • требование не касается профиля клиента → нет\n"
    "  • требование не упомянуто → нет\n\n"
    "Ответь только одним словом: да или нет"
)


async def check_tenant_conflict(tenant_requirements: str, client_notes: str) -> bool:
    """
    Second LLM call: checks if listing tenant requirements conflict with client notes.
    Only called when both fields are non-empty. Returns True if conflict found.
    """
    answer = await _groq_request(
        messages=[
            {"role": "system", "content": _CONFLICT_SYSTEM},
            {"role": "user", "content": (
                f"Требования объявления: {tenant_requirements}\n"
                f"Заметки о клиенте: {client_notes}\n\n"
                "Объявление ЗАПРЕЩАЕТ этому клиенту? Ответь только: да или нет"
            )},
        ],
        max_tokens=5,
        temperature=0.0,
    )
    if not answer:
        return False  # при ошибке не блокируем
    return answer.lower().startswith("да")


def check_match(listing: dict, client_obj: Client) -> tuple[bool, int]:
    """
    Check if a listing matches a client's requirements.
    Returns (matches: bool, score: int 0-100).

    Euro format rule: Евро2к matches clients looking for 1к or 2к, etc.
    euro_rooms_range=[N-1, N] means the listing suits N-1 and N room seekers.

    Tenant conflict (БЖ/БД/nationality) is checked separately via check_tenant_conflict().
    """
    if not listing.get("is_listing"):
        return False, 0

    score = 0
    total_criteria = 0

    # property type — hard filter
    if client_obj.property_type:
        total_criteria += 20
        if listing.get("property_type") == client_obj.property_type:
            score += 20
        else:
            return False, 0

    # rooms — with euro format expansion
    if client_obj.min_rooms or client_obj.max_rooms:
        total_criteria += 15
        rooms = listing.get("rooms")
        if rooms is not None:
            euro_range = listing.get("euro_rooms_range")
            if euro_range and len(euro_range) == 2:
                # Euro apartment: compatible with euro_range[0] and euro_range[1] rooms
                lo, hi = euro_range
                client_min = client_obj.min_rooms or 0
                client_max = client_obj.max_rooms or 99
                # passes if client's range overlaps [lo, hi]
                if client_max < lo or client_min > hi:
                    return False, 0
            else:
                if client_obj.min_rooms and rooms < client_obj.min_rooms:
                    return False, 0
                if client_obj.max_rooms and rooms > client_obj.max_rooms:
                    return False, 0
            score += 15

    # price — hard filter with 5% tolerance
    if client_obj.min_price or client_obj.max_price:
        total_criteria += 20
        price = listing.get("price")
        if price is not None:
            if client_obj.min_price and price < client_obj.min_price * 0.95:
                return False, 0
            if client_obj.max_price and price > client_obj.max_price * 1.05:
                return False, 0
            score += 20

    # area — hard filter with 10% tolerance
    if client_obj.min_area or client_obj.max_area:
        total_criteria += 15
        area = listing.get("area")
        if area is not None:
            if client_obj.min_area and area < client_obj.min_area * 0.9:
                return False, 0
            if client_obj.max_area and area > client_obj.max_area * 1.1:
                return False, 0
            score += 15

    # district — strict hard filter. Unknown district is normalized to "Пригород"
    # by enrich_district(), so listing.district is always set when is_listing=true.
    # Client with no districts selected = "Все районы" = passes everything.
    if client_obj.districts:
        total_criteria += 10
        district = listing.get("district") or "Пригород"
        matched = any(
            d.lower() in district.lower() or district.lower() in d.lower()
            for d in client_obj.districts
        )
        if not matched:
            return False, 0
        score += 10

    if total_criteria == 0:
        return True, 50

    final_score = int(score / total_criteria * 100) if total_criteria else 50
    return final_score >= 40, final_score
