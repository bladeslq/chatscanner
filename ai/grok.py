import json
import re
import httpx
from openai import AsyncOpenAI
from config import GROK_API_KEY, GROK_BASE_URL, GROK_MODEL
from database.models import Client

# Kaspersky intercepts TLS on Windows — disable cert verification
_http_client = httpx.AsyncClient(verify=False)
client = AsyncOpenAI(api_key=GROK_API_KEY, base_url=GROK_BASE_URL, http_client=_http_client)

SYSTEM_PROMPT = """Ты — профессиональный аналитик рынка недвижимости Казани. Извлекаешь структурированные данные из сообщений Telegram-чатов риелторов.

## Районы Казани
Советский, Приволжский, Кировский, Вахитовский, Авиастроительный, Ново-Савиновский, Московский.
Ты знаешь все улицы и ЖК Казани и определяешь район по ним. Если улица или ЖК могут быть в нескольких районах — указывай первый наиболее вероятный.

## Словарь сокращений казанских риелторов

Тип жилья:
- 1к / 2к / 3к — количество комнат
- Евро1к / Евро2к / Евро3к — кухня-гостиная + спальни (Евро2к = 2 комнаты)
- Студия / Гост / Гостинка — однокомнатная (rooms=1, euro_format=true)

Состояние:
- Простая — обычный ремонт
- Дрова — плохое состояние
- Хрущ — хрущёвка
- Упак — полная комплектация мебелью и техникой
- Пуля / Пушка — отличное предложение

Условия оплаты:
- 50ку или 50 + ку — 50 тыс. + коммунальные услуги (price_includes_utilities=false)
- 50сч или 50 + сч — 50 тыс. + счётчики (price_includes_utilities=false)
- 50вв или 50 всё включено — всё включено, КУ=0 (price_includes_utilities=true)

Залог:
- залог 100% — залог равен цене аренды
- залог 50% — залог равен половине цены
- "можно разбить" / "в рассрочку" — залог делимый (deposit_negotiable=true)

Комиссия:
- С+ — деление комиссии на 3 и более риелторов

Требования к жильцам:
- СП — семейная пара
- БД — без детей
- БЖ — без животных
- Наши — граждане РФ (русские / татары)
- СНГ — граждане СНГ
- Ино — иностранцы (дальнее зарубежье)

## Правила извлечения ЖК
- Название ЖК часто пишут без префикса "ЖК" — отдельной строкой или после типа квартиры
- Примеры: "Максат", "Светлая долина", "Солнечный город" — это названия ЖК, запиши в complex
- Если видишь слово похожее на название жилого комплекса — запиши его в complex даже без "ЖК" перед ним

## Правила извлечения
- Евро2к считай как rooms=2 с euro_format=true
- Студию / гостинку считай как rooms=1 с euro_format=true
- Площадь "60кв" / "60м" / "60 метров" → 60
- Залог в % пересчитывай в рубли от основной цены
- Если указано "вв" → price_includes_utilities=true
- Верни ТОЛЬКО JSON, без пояснений и markdown-блоков"""

EXTRACTION_PROMPT = """Проанализируй сообщение из Telegram-чата риелторов Казани.

Если это НЕ объявление о недвижимости — верни: {"is_listing": false}

Если это объявление — верни JSON строго в таком формате:
{
  "is_listing": true,
  "transaction_type": "sale" | "rent",
  "property_type": "apartment" | "house" | "commercial" | "land" | "room",
  "rooms": число или null,
  "euro_format": true | false,
  "price": число в рублях или null,
  "price_includes_utilities": true | false | null,
  "deposit": число в рублях или null,
  "deposit_negotiable": true | false | null,
  "area": число м² или null,
  "district": "район Казани" или null,
  "address": "улица и дом" или null,
  "complex": "название ЖК" или null,
  "floor": число или null,
  "floors_total": число или null,
  "tenant_requirements": "требования к жильцам строкой" или null,
  "commission_shared": true | false | null,
  "description": "краткое описание объекта",
  "contact": "имя, телефон или @ник" или null
}

Сообщение:
"""


async def extract_listing(message_text: str) -> dict:
    """Extract real estate listing data from a Telegram message."""
    if len(message_text.strip()) < 20:
        return {"is_listing": False}
    try:
        response = await client.chat.completions.create(
            model=GROK_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": EXTRACTION_PROMPT + message_text},
            ],
            temperature=0.2,
            max_tokens=800,
        )
        content = response.choices[0].message.content.strip()
        # extract JSON even if wrapped in ```json blocks
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if match:
            return json.loads(match.group())
        return {"is_listing": False}
    except Exception:
        return {"is_listing": False}


def check_match(listing: dict, client_obj: Client) -> tuple[bool, int]:
    """
    Programmatically check if a listing matches a client's requirements.
    Returns (matches: bool, score: int 0-100).
    """
    if not listing.get("is_listing"):
        return False, 0

    score = 0
    total_criteria = 0

    # transaction type
    if client_obj.transaction_type:
        total_criteria += 20
        if listing.get("transaction_type") == client_obj.transaction_type:
            score += 20

    # property type
    if client_obj.property_type:
        total_criteria += 20
        if listing.get("property_type") == client_obj.property_type:
            score += 20
        else:
            return False, 0  # hard mismatch

    # rooms
    if client_obj.min_rooms or client_obj.max_rooms:
        total_criteria += 15
        rooms = listing.get("rooms")
        if rooms is not None:
            if client_obj.min_rooms and rooms < client_obj.min_rooms:
                return False, 0
            if client_obj.max_rooms and rooms > client_obj.max_rooms:
                return False, 0
            score += 15

    # price
    if client_obj.min_price or client_obj.max_price:
        total_criteria += 20
        price = listing.get("price")
        if price is not None:
            if client_obj.min_price and price < client_obj.min_price * 0.95:
                return False, 0
            if client_obj.max_price and price > client_obj.max_price * 1.05:
                return False, 0
            score += 20

    # area
    if client_obj.min_area or client_obj.max_area:
        total_criteria += 15
        area = listing.get("area")
        if area is not None:
            if client_obj.min_area and area < client_obj.min_area * 0.9:
                return False, 0
            if client_obj.max_area and area > client_obj.max_area * 1.1:
                return False, 0
            score += 15

    # district — hard filter: if client specified districts and listing district is known,
    # it must match at least one; unknown district passes through
    if client_obj.districts:
        total_criteria += 10
        district = listing.get("district", "")
        if district:
            matched = any(
                d.lower() in district.lower() or district.lower() in d.lower()
                for d in client_obj.districts
            )
            if not matched:
                return False, 0
            score += 10

    if total_criteria == 0:
        return True, 50  # no criteria — all listings match

    final_score = int(score / total_criteria * 100) if total_criteria else 50
    return final_score >= 40, final_score
