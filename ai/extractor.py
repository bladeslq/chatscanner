"""Haiku 4.5 extraction — replaces ai/grok.py.

Single LLM call per unique message text (caller-side caching). Prompt cache on
the system message reduces cost ~10x for repeated calls.
"""
import asyncio
import json
import re
import logging
import httpx
from anthropic import AsyncAnthropic
from config import ANTHROPIC_API_KEY, HAIKU_MODEL

logger = logging.getLogger(__name__)

# Kaspersky intercepts TLS on Windows — keep verify=False for parity with Groq client
_http_client = httpx.AsyncClient(verify=False)
_client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY, http_client=_http_client)

# Serialize calls to avoid hammering rate limits during bursts
_sem = asyncio.Semaphore(3)

# Pause flag (mirrors grok pattern): on rate limit / repeated errors,
# pause briefly and skip subsequent callers cheaply.
_paused_until: float = 0.0
_PAUSE_BASE = 30.0
_PAUSE_MAX = 300.0
_next_pause: float = _PAUSE_BASE


def is_paused() -> bool:
    loop = asyncio.get_event_loop()
    return loop.time() < _paused_until


def _trigger_pause() -> float:
    global _paused_until, _next_pause
    loop = asyncio.get_event_loop()
    duration = _next_pause
    _paused_until = loop.time() + duration
    _next_pause = min(_next_pause * 2, _PAUSE_MAX)
    return duration


def _reset_pause() -> None:
    global _next_pause
    _next_pause = _PAUSE_BASE


SYSTEM = """Ты извлекаешь данные из объявлений об аренде квартир в Казани.

Возвращаешь СТРОГО JSON без markdown:
{
  "is_listing": true/false,
  "property_type": "apartment"|"room"|null,
  "rooms": число|null,
  "euro_format": true|false,
  "euro_rooms_range": [мин, макс]|null,
  "price": число в рублях|null,
  "price_includes_utilities": true|false|null,
  "deposit": число в рублях|null,
  "deposit_negotiable": true|false|null,
  "area": число м²|null,
  "floor": число|null,
  "floors_total": число|null,
  "address": "улица и дом если есть"|null,
  "complex": "название ЖК"|null,
  "district_hint": "район если упомянут"|null,
  "district_candidates": ["район1","район2"]|null,
  "building_type": "хрущёвка"|"сталинка"|"брежневка"|"новостройка"|"панель"|null,
  "owner_type": "owner"|"agent"|null,
  "has_keys": true|false|null,
  "condition": "упак"|"простая"|"дрова"|"пуля"|null,
  "available_until": "строка срока"|null,
  "commission_percent": число|null,
  "kickback_percent": число|null,
  "commission_shared": true|false|null,
  "tenant_requirements": "требования к жильцам как в тексте"|null,
  "description": "краткое описание: техника, мебель, инфраструктура, особенности",
  "contact": "имя, телефон или @ник"|null
}

## ОСНОВНЫЕ ПРАВИЛА

- is_listing=false ТОЛЬКО для домов, коттеджей, таунхаусов, дач, продажи участков, коммерции
- ВАЖНО: 2-этажная (или N-этажная) КВАРТИРА — это apartment! Не путай с домом. Если в тексте есть "квартира" / "Е2к" / "студия" / "1к" / "2к" — это apartment, даже если "2-этажная" или "двухуровневая"
- Дом/коттедж — отдельное здание (упоминаются "дом", "коттедж", "участок Х соток", "снт", "посёлок", "ИЖС")
- "30+ку" / "30т" / "30 000" / "30000" → price=30000
- "30вв" / "30 ВВ" / "35 всё включено" → price выставляй (КУ включена), price_includes_utilities=true
- Студия / Гостинка → property_type=apartment, rooms=1
- "Евро2к" / "Е2к" → property_type=apartment, rooms=2, euro_format=true, euro_rooms_range=[1,2]
- "Евро3к" / "Е3к" → property_type=apartment, rooms=3, euro_format=true, euro_rooms_range=[2,3]
- "Евро4к" / "Е4к" → property_type=apartment, rooms=4, euro_format=true, euro_rooms_range=[3,4]
- Обычная квартира (не евро) → euro_format=false, euro_rooms_range=null

## АДРЕС

- "Айдарова 18" → address="Айдарова 18"
- "Сахарова" без дома → address="Сахарова"
- Перекрёсток "Ч.Айтматова д.1/ Фучика/ Ломжинская" → address="Ч.Айтматова 1" (берём ПЕРВУЮ улицу с её домом, остальное — пересечения, отбрасываем)
- "А. Еники/ Вишневского 2/53" → address="А. Еники 2"
- НЕ галлюцинируй адрес. Если адреса нет — null

## СОКРАЩЕНИЯ УЛИЦ — ОБЯЗАТЕЛЬНО РАСКРЫВАЙ

- "Ком.Габишева" / "К.Габишева" → "Комиссара Габишева"
- "А.Еники" / "А. Еники" → "Адель Еники"
- "Ч.Айтматова" / "Ч. Айтматова" → "Чингиза Айтматова"
- "Г.Тукая" / "Г. Тукая" → "Габдуллы Тукая"
- "М.Джалиля" / "М. Джалиля" → "Мусы Джалиля"
- "Х.Такташа" / "Х. Такташа" → "Хади Такташа"
- "К.Маркса" → "Карла Маркса"
- "Н.Ершова" → "Николая Ершова"
- "Я.Гашека" → "Ярослава Гашека"
- "А.Кутуя" → "Адиля Кутуя"
- "Ф.Амирхана" → "Фатиха Амирхана"
- "Ш.Усманова" → "Шамиля Усманова"
- "С.Сайдашева" → "Салиха Сайдашева"

## ЛЕНДМАРКИ — НЕ ИЗВЛЕКАЙ КАК АДРЕС/ЖК

"рядом X" / "напротив X" / "около X" / "у X" / "за X" / "возле X" — это ОРИЕНТИР, не сам объект.
- "Напротив Меги" → НЕ адрес
- "Рядом ЖК Максат" → ЖК этого объявления НЕ Максат, complex=null
- "Около Салават Купере" → complex=null если только сам не находится в нём
- "Рядом метро Козья Слобода" → НЕ адрес и НЕ район

ЖК извлекай ТОЛЬКО если в тексте явно сказано что объект находится в этом ЖК ("ЖК X", "в ЖК X", "сдаю в X", "квартира в Х").

## ЖК

- Без префикса "ЖК": "Сдам в ЖК Лето" → complex="Лето"

## СЛОВАРЬ АББРЕВИАТУР РИЕЛТОРОВ КАЗАНИ

- Соб / Собственник → owner_type="owner"
- Ключи / есть ключи → has_keys=true
- Упак — полностью укомплектована → condition="упак"
- Простая — обычный ремонт → condition="простая"
- Дрова — плохое состояние → condition="дрова"
- Пуля / Пушка / Бомба — отличное соотношение → condition="пуля"
- Хрущ / Хрущёвка → building_type="хрущёвка"
- Сталинка → building_type="сталинка"
- Брежневка / Ленинградка → building_type="брежневка"
- 50ку / 50+ку → price=50000, price_includes_utilities=false
- 50вв / 50 ВВ → price=50000, price_includes_utilities=true
- Залог 100% → deposit = price
- Залог 50% / залог ½ → deposit = price / 2
- Делимый / можно разбить / рассрочка → deposit_negotiable=true
- Без залога → deposit=0
- Комиссия 50% → commission_percent=50
- Откат / отк / от — commission_percent / kickback_percent в зависимости от контекста
- С+ / 50/50 → commission_shared=true (это про комиссию, НЕ ЖК!)

## ПРИГОРОДНЫЕ ПОСЁЛКИ → district_hint

Если в тексте упомянут пригородный посёлок — обязательно ставь правильный район:
- "Новая Тура" / "Осиново" / "Айша" / "Олуяз" → "Зеленодольский"
- "Куюки" / "Усады" / "Царёво" / "Богородское" → "Пестречинский"
- "Малые Кабаны" / "Большие Кабаны" / "Сокуры" → "Лаишевский"
- "Высокая Гора" / "Шапши" / "Семиозерка" → "Высокогорский"

## DISTRICT_CANDIDATES — улица в нескольких районах Казани

Если в адресе указана только улица БЕЗ номера дома, и ты знаешь что эта улица идёт через несколько районов Казани → верни массив всех районов в `district_candidates`. district_hint при этом null.

Примеры улиц Казани, идущих через несколько районов:
- "Проспект Победы" → ["Советский", "Приволжский"]
- "Декабристов" → ["Московский", "Ново-Савиновский"]
- "Бутлерова" → ["Вахитовский", "Приволжский"]
- "Габдуллы Тукая" → ["Вахитовский", "Приволжский"]
- "Ленина" → ["Вахитовский", "Кировский"]

ВАЖНО:
- Если есть номер дома → district_candidates=null
- Знаешь точно один район — district_hint, district_candidates=null
- Улица в нескольких районах → district_hint=null, district_candidates=массив
- Не знаешь — оба null

## TENANT_REQUIREMENTS

Копируй фразу из текста как есть: "наших, без животных", "БЖ", "только семьи", "иностранцев можно", "только девочек", "БД БЖ" и т.д.

## ПРИМЕРЫ ИЗВЛЕЧЕНИЯ

Пример 1.
Текст: "Сдам 1к, ул. Айдарова 18, 35+ку, залог 50%, упак, ключи, БЖ, наших. 89XXXXXXXXX Алина".
JSON: {"is_listing": true, "property_type": "apartment", "rooms": 1, "euro_format": false, "euro_rooms_range": null, "price": 35000, "price_includes_utilities": false, "deposit": 17500, "deposit_negotiable": false, "area": null, "floor": null, "floors_total": null, "address": "Айдарова 18", "complex": null, "district_hint": "Авиастроительный", "district_candidates": null, "building_type": null, "owner_type": null, "has_keys": true, "condition": "упак", "available_until": null, "commission_percent": null, "kickback_percent": null, "commission_shared": null, "tenant_requirements": "БЖ, наших", "description": "укомплектована, ключи на руках", "contact": "Алина 89XXXXXXXXX"}

Пример 2.
Текст: "Сдается двухуровневая 3к квартира в ЖК Лето, 90м², 5/9, 65 ВВ, делимый, можно с детьми и кошкой. С+. Соб. 89XXXXXXXXX".
JSON: {"is_listing": true, "property_type": "apartment", "rooms": 3, "euro_format": false, "euro_rooms_range": null, "price": 65000, "price_includes_utilities": true, "deposit": null, "deposit_negotiable": true, "area": 90, "floor": 5, "floors_total": 9, "address": null, "complex": "Лето", "district_hint": null, "district_candidates": null, "building_type": null, "owner_type": "owner", "has_keys": null, "condition": null, "available_until": null, "commission_percent": null, "kickback_percent": null, "commission_shared": true, "tenant_requirements": "можно с детьми и кошкой", "description": "двухуровневая, всё включено", "contact": "89XXXXXXXXX"}

Пример 3 (перекрёсток — берём первую улицу с её домом).
Текст: "Сдаю 2к Ч.Айтматова д.1/ Фучика/ Ломжинская, 2/16, 40т+ку, упак, БЖ БД 89XXXXXXXXX".
JSON: {"is_listing": true, "property_type": "apartment", "rooms": 2, "euro_format": false, "euro_rooms_range": null, "price": 40000, "price_includes_utilities": false, "deposit": null, "deposit_negotiable": null, "area": null, "floor": 2, "floors_total": 16, "address": "Чингиза Айтматова 1", "complex": null, "district_hint": null, "district_candidates": null, "building_type": null, "owner_type": null, "has_keys": null, "condition": "упак", "available_until": null, "commission_percent": null, "kickback_percent": null, "commission_shared": null, "tenant_requirements": "БЖ БД", "description": null, "contact": "89XXXXXXXXX"}

Пример 4 (улица в нескольких районах, нет дома → district_candidates).
Текст: "Сдам 1к на Бутлерова, 35м², 4/9, 30+ку, БЖ. Соб 89XXXXXXXXX".
JSON: {"is_listing": true, "property_type": "apartment", "rooms": 1, "euro_format": false, "euro_rooms_range": null, "price": 30000, "price_includes_utilities": false, "deposit": null, "deposit_negotiable": null, "area": 35, "floor": 4, "floors_total": 9, "address": "Бутлерова", "complex": null, "district_hint": null, "district_candidates": ["Вахитовский", "Приволжский"], "building_type": null, "owner_type": "owner", "has_keys": null, "condition": null, "available_until": null, "commission_percent": null, "kickback_percent": null, "commission_shared": null, "tenant_requirements": "БЖ", "description": null, "contact": "89XXXXXXXXX"}

Пример 5 (Е2к — евро-формат с диапазоном).
Текст: "Сдам Е2к в ЖК Светлая Долина, 45м², 7/12, 38000+ку, упак, ключи, можно ино. 89XXXXXXXXX".
JSON: {"is_listing": true, "property_type": "apartment", "rooms": 2, "euro_format": true, "euro_rooms_range": [1, 2], "price": 38000, "price_includes_utilities": false, "deposit": null, "deposit_negotiable": null, "area": 45, "floor": 7, "floors_total": 12, "address": null, "complex": "Светлая Долина", "district_hint": "Советский", "district_candidates": null, "building_type": null, "owner_type": null, "has_keys": true, "condition": "упак", "available_until": null, "commission_percent": null, "kickback_percent": null, "commission_shared": null, "tenant_requirements": "можно ино", "description": "укомплектована, ключи", "contact": "89XXXXXXXXX"}

Пример 6 (НЕ объявление — продажа).
Текст: "Продаю 2к в Ново-Савиновском, 6.5 млн, торг".
JSON: {"is_listing": false}

Пример 7 (НЕ объявление — поиск).
Текст: "Ищу 1к до 30к в Вахитовском районе, парень, без животных".
JSON: {"is_listing": false}

Пример 8 (лендмарк, не сам ЖК).
Текст: "Сдам студию рядом с ЖК Максат, ул. Гаврилова 28, 5/16, 25+ку".
JSON: {"is_listing": true, "property_type": "apartment", "rooms": 1, "euro_format": false, "euro_rooms_range": null, "price": 25000, "price_includes_utilities": false, "deposit": null, "deposit_negotiable": null, "area": null, "floor": 5, "floors_total": 16, "address": "Гаврилова 28", "complex": null, "district_hint": null, "district_candidates": null, "building_type": null, "owner_type": null, "has_keys": null, "condition": null, "available_until": null, "commission_percent": null, "kickback_percent": null, "commission_shared": null, "tenant_requirements": null, "description": null, "contact": null}
"""


async def extract_listing(text: str) -> dict:
    """Extract structured listing data from raw Telegram message."""
    if not ANTHROPIC_API_KEY:
        logger.warning("ANTHROPIC_API_KEY missing — extraction disabled")
        return {"is_listing": False}

    if len(text.strip()) < 20:
        return {"is_listing": False}

    if is_paused():
        return {"is_listing": False}

    async with _sem:
        for attempt in range(3):
            try:
                msg = await _client.messages.create(
                    model=HAIKU_MODEL,
                    max_tokens=900,
                    system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
                    messages=[{"role": "user", "content": f"Объявление:\n{text}\n\nВерни JSON."}],
                )
                _reset_pause()
                raw = msg.content[0].text.strip()
                m = re.search(r"\{.*\}", raw, re.DOTALL)
                if not m:
                    return {"is_listing": False}
                try:
                    return json.loads(m.group())
                except json.JSONDecodeError:
                    return {"is_listing": False}
            except Exception as e:
                msg_str = str(e)
                if "429" in msg_str or "rate" in msg_str.lower():
                    if attempt < 2:
                        await asyncio.sleep(2 ** attempt * 3)
                        continue
                    duration = _trigger_pause()
                    logger.warning(f"Anthropic 429 — pausing extraction for {duration:.0f}s")
                    return {"is_listing": False}
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt * 2)
                    continue
                duration = _trigger_pause()
                logger.warning(f"Anthropic transient error — pausing {duration:.0f}s: {e}")
                return {"is_listing": False}
    return {"is_listing": False}
