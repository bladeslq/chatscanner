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
  "district_hint": "район ТОЛЬКО для пригородов / городских ориентиров / явно названного района"|null,
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
- ВАЖНО: 2-этажная (или N-этажная) КВАРТИРА — это apartment! Не путай с домом. Если в тексте есть "квартира" / "Е2к" / "студия" / "1к" / "2к" / "1кк" / "2кк" / "3кк" / "4кк" — это apartment, даже если "2-этажная" или "двухуровневая"
- Казанский сленг "Nкк" (двойное «к») = N-комнатная КВАРТИРА: "1кк"=1 комната, "2кк"=2 комнаты, "3кк"=3 комнаты, "4кк"=4 комнаты. Это НЕ дом, НЕ коттедж — всегда property_type=apartment, rooms=N, euro_format=false. Даже если в тексте больше нет слова "квартира" — наличия "Nкк" достаточно для is_listing=true.
- Дом/коттедж — отдельное здание (упоминаются "дом", "коттедж", "участок Х соток", "снт", "посёлок", "ИЖС")
- "30+ку" / "30т" / "30 000" / "30000" → price=30000
- "30вв" / "30 ВВ" / "35 всё включено" → price выставляй (КУ включена), price_includes_utilities=true
- Студия / Гостинка → property_type=apartment, rooms=1
- "Евро2к" / "Е2к" → property_type=apartment, rooms=2, euro_format=true, euro_rooms_range=[1,2]
- "Евро3к" / "Е3к" → property_type=apartment, rooms=3, euro_format=true, euro_rooms_range=[2,3]
- "Евро4к" / "Е4к" → property_type=apartment, rooms=4, euro_format=true, euro_rooms_range=[3,4]
- Обычная квартира (не евро) → euro_format=false, euro_rooms_range=null
- Опечатки в единицах площади ("кВм", "кв.м", "м2", "м²", "квм") — все означают квадратные метры, извлекай area нормально

## АДРЕС

- "Айдарова 18" → address="Айдарова 18"
- "Сахарова" без дома → address="Сахарова"
- Перекрёсток "Ч.Айтматова д.1/ Фучика/ Ломжинская" → address="Ч.Айтматова 1" (берём ПЕРВУЮ улицу с её домом, остальное — пересечения, отбрасываем)
- "А. Еники/ Вишневского 2/53" → address="А. Еники 2"
- Перекрёсток БЕЗ номера дома "ул Гвардейская/Аделя Кутуя" → address="Гвардейская" (берём ПЕРВУЮ улицу как имя улицы, без слешей и второй улицы)
- Перекрёсток БЕЗ дома "Декабристов/Чистопольская" → address="Декабристов"
- НЕ галлюцинируй адрес. Если адреса нет — null
- Очевидные опечатки исправляй: "амирхона" → "Амирхана", "айдаровп" → "Айдарова". Если опечатка не очевидна — оставь как есть, не выдумывай.

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
- Составное имя бери ЦЕЛИКОМ, не обрезай. "ЖК Светлая Долина", "ЖК Каскад Амирхана", "ЖК Мой Ритм 2-я очередь" — это полные имена.
- Если в названии ЖК есть улица, на которой он стоит (типичная казанская практика: "ЖК Каскад Амирхана") — это часть имени ЖК, а не адрес.
  - "ЖК Каскад Амирхана 12" → complex="Каскад Амирхана", address="Амирхана 12"
  - "ЖК Чаша Абсалямова 13" → complex="Чаша", address="Абсалямова 13"  ("Чаша" — имя ЖК, "Абсалямова 13" — адрес)
- Латиницу транслитерируй в кириллицу: "Art City" → "Арт Сити", "Light House" → "Лайт Хаус", "Clover House" → "Кловер Хаус", "KazanMall" → "Казань Молл".
- Очевидные опечатки исправляй: "Касскад" → "Каскад", "Светлоя Долина" → "Светлая Долина". Не очевидные — оставь как есть.
- НЕ галлюцинируй ЖК. Если имя ЖК не упомянуто в тексте — complex=null. "Студия в новостройке" → complex=null, не угадывай.

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

## ГОРОДСКИЕ ОРИЕНТИРЫ И СЛЕНГ → district_hint

Используй ТОЛЬКО когда в тексте нет улицы и нет ЖК, а есть только народное название места/района. Закрытый список:
- "Соцгород" / "Жилплощадка" / "у НКЦ" / "Адмиралтейка" / "Караваево" → "Кировский"
- "Дербышки" / "Аметьево" / "Танкодром" / "Горки" / "Азино" / "Светлая поляна" → "Советский"
- "Жилка" / "у Чаши" / "Караваево пос. Северный" / "Восстания" → "Ново-Савиновский"
- "Авиастрой" / "Караваево пос. Сухая река" / "Северный вокзал" / "Соцгород Авиастр." → "Авиастроительный"
- "Старо-Татарская слобода" / "Кремль" / "Туфана Миннуллина" → "Вахитовский"
- "Кварталы" / "Закабанье" / "Мирный" / "Аракчино" → "Приволжский"
- "Северный мост" / "Юдино" / "Залесный" → "Московский"

Если ориентира из этого списка в тексте нет — district_hint=null.

## DISTRICT_CANDIDATES — улица в нескольких районах Казани

ЗАКРЫТЫЙ СПИСОК. Заполняй `district_candidates` массивом ТОЛЬКО если улица из списка ниже И в адресе НЕТ номера дома. district_hint при этом null. Любые другие улицы (даже если ты считаешь их multi-district) → district_candidates=null, район определит геокодер.

Полный whitelist multi-district улиц Казани (проверено через 2GIS на реальных домах):
- "Бондаренко" → ["Московский", "Ново-Савиновский"]
- "Восстания" → ["Московский", "Ново-Савиновский"]
- "Габдуллы Тукая" → ["Вахитовский", "Приволжский"]
- "Гагарина" → ["Ново-Савиновский", "Московский"]
- "Ибрагимова" → ["Московский", "Ново-Савиновский"]
- "Назарбаева" → ["Вахитовский", "Приволжский"]
- "Николая Ершова" → ["Вахитовский", "Советский"]
- "Проспект Победы" → ["Приволжский", "Советский"]
- "Рихарда Зорге" → ["Приволжский", "Советский"]
- "Сабан" → ["Кировский", "Московский"]
- "Хади Такташа" → ["Вахитовский", "Приволжский"]
- "Чистопольская" → ["Московский", "Ново-Савиновский"]
- "Юлиуса Фучика" → ["Приволжский", "Советский"]
- "Ямашева" → ["Московский", "Ново-Савиновский"]

## КОГДА ЗАПОЛНЯТЬ district_hint — ЗАКРЫТЫЙ СПИСОК

Заполняй `district_hint` ТОЛЬКО в одном из этих случаев:
1. В тексте явно назван район словом: "в Кировском", "Вахитовский район", "Приволжский".
2. Упомянут пригородный посёлок из списка выше.
3. Упомянут городской ориентир/сленг из закрытого списка выше — И при этом в тексте НЕТ улицы и НЕТ ЖК.

ВО ВСЕХ ОСТАЛЬНЫХ СЛУЧАЯХ — `district_hint=null`. В частности:
- Есть улица + номер дома → district_hint=null. Пусть район определит геокодер по адресу.
- Есть имя ЖК → district_hint=null. Пусть район определит геокодер по ЖК.
- Есть улица без дома → district_hint=null. Если улица идёт через несколько районов — заполни district_candidates (см. ниже). Иначе оба null.
- Сомневаешься — оба null. НЕ угадывай район по своим знаниям географии Казани.

Правила для district_candidates (без изменений):
- Если есть номер дома → district_candidates=null
- Улица в нескольких районах без дома → district_candidates=массив, district_hint=null
- Не знаешь — оба null

## TENANT_REQUIREMENTS

Копируй фразу из текста как есть: "наших, без животных", "БЖ", "только семьи", "иностранцев можно", "только девочек", "БД БЖ" и т.д.

## ПРИМЕРЫ ИЗВЛЕЧЕНИЯ

Пример 1 (есть улица+дом → district_hint=null, район определит геокодер).
Текст: "Сдам 1к, ул. Айдарова 18, 35+ку, залог 50%, упак, ключи, БЖ, наших. 89XXXXXXXXX Алина".
JSON: {"is_listing": true, "property_type": "apartment", "rooms": 1, "euro_format": false, "euro_rooms_range": null, "price": 35000, "price_includes_utilities": false, "deposit": 17500, "deposit_negotiable": false, "area": null, "floor": null, "floors_total": null, "address": "Айдарова 18", "complex": null, "district_hint": null, "district_candidates": null, "building_type": null, "owner_type": null, "has_keys": true, "condition": "упак", "available_until": null, "commission_percent": null, "kickback_percent": null, "commission_shared": null, "tenant_requirements": "БЖ, наших", "description": "укомплектована, ключи на руках", "contact": "Алина 89XXXXXXXXX"}

Пример 2.
Текст: "Сдается двухуровневая 3к квартира в ЖК Лето, 90м², 5/9, 65 ВВ, делимый, можно с детьми и кошкой. С+. Соб. 89XXXXXXXXX".
JSON: {"is_listing": true, "property_type": "apartment", "rooms": 3, "euro_format": false, "euro_rooms_range": null, "price": 65000, "price_includes_utilities": true, "deposit": null, "deposit_negotiable": true, "area": 90, "floor": 5, "floors_total": 9, "address": null, "complex": "Лето", "district_hint": null, "district_candidates": null, "building_type": null, "owner_type": "owner", "has_keys": null, "condition": null, "available_until": null, "commission_percent": null, "kickback_percent": null, "commission_shared": true, "tenant_requirements": "можно с детьми и кошкой", "description": "двухуровневая, всё включено", "contact": "89XXXXXXXXX"}

Пример 3 (перекрёсток — берём первую улицу с её домом).
Текст: "Сдаю 2к Ч.Айтматова д.1/ Фучика/ Ломжинская, 2/16, 40т+ку, упак, БЖ БД 89XXXXXXXXX".
JSON: {"is_listing": true, "property_type": "apartment", "rooms": 2, "euro_format": false, "euro_rooms_range": null, "price": 40000, "price_includes_utilities": false, "deposit": null, "deposit_negotiable": null, "area": null, "floor": 2, "floors_total": 16, "address": "Чингиза Айтматова 1", "complex": null, "district_hint": null, "district_candidates": null, "building_type": null, "owner_type": null, "has_keys": null, "condition": "упак", "available_until": null, "commission_percent": null, "kickback_percent": null, "commission_shared": null, "tenant_requirements": "БЖ БД", "description": null, "contact": "89XXXXXXXXX"}

Пример 4 (улица в нескольких районах, нет дома → district_candidates).
Текст: "Сдам 1к на Бутлерова, 35м², 4/9, 30+ку, БЖ. Соб 89XXXXXXXXX".
JSON: {"is_listing": true, "property_type": "apartment", "rooms": 1, "euro_format": false, "euro_rooms_range": null, "price": 30000, "price_includes_utilities": false, "deposit": null, "deposit_negotiable": null, "area": 35, "floor": 4, "floors_total": 9, "address": "Бутлерова", "complex": null, "district_hint": null, "district_candidates": ["Вахитовский", "Приволжский"], "building_type": null, "owner_type": "owner", "has_keys": null, "condition": null, "available_until": null, "commission_percent": null, "kickback_percent": null, "commission_shared": null, "tenant_requirements": "БЖ", "description": null, "contact": "89XXXXXXXXX"}

Пример 5 (Е2к — евро-формат с диапазоном; есть ЖК → district_hint=null).
Текст: "Сдам Е2к в ЖК Светлая Долина, 45м², 7/12, 38000+ку, упак, ключи, можно ино. 89XXXXXXXXX".
JSON: {"is_listing": true, "property_type": "apartment", "rooms": 2, "euro_format": true, "euro_rooms_range": [1, 2], "price": 38000, "price_includes_utilities": false, "deposit": null, "deposit_negotiable": null, "area": 45, "floor": 7, "floors_total": 12, "address": null, "complex": "Светлая Долина", "district_hint": null, "district_candidates": null, "building_type": null, "owner_type": null, "has_keys": true, "condition": "упак", "available_until": null, "commission_percent": null, "kickback_percent": null, "commission_shared": null, "tenant_requirements": "можно ино", "description": "укомплектована, ключи", "contact": "89XXXXXXXXX"}

Пример 6 (НЕ объявление — продажа).
Текст: "Продаю 2к в Ново-Савиновском, 6.5 млн, торг".
JSON: {"is_listing": false}

Пример 7 (НЕ объявление — поиск).
Текст: "Ищу 1к до 30к в Вахитовском районе, парень, без животных".
JSON: {"is_listing": false}

Пример 8 (лендмарк, не сам ЖК).
Текст: "Сдам студию рядом с ЖК Максат, ул. Гаврилова 28, 5/16, 25+ку".
JSON: {"is_listing": true, "property_type": "apartment", "rooms": 1, "euro_format": false, "euro_rooms_range": null, "price": 25000, "price_includes_utilities": false, "deposit": null, "deposit_negotiable": null, "area": null, "floor": 5, "floors_total": 16, "address": "Гаврилова 28", "complex": null, "district_hint": null, "district_candidates": null, "building_type": null, "owner_type": null, "has_keys": null, "condition": null, "available_until": null, "commission_percent": null, "kickback_percent": null, "commission_shared": null, "tenant_requirements": null, "description": null, "contact": null}

Пример 9 (пригород без улицы → hint допустим из закрытого списка).
Текст: "Сдам 1к в Куюках, новостройка, 18+ку, БЖ. 89XXXXXXXXX".
JSON: {"is_listing": true, "property_type": "apartment", "rooms": 1, "euro_format": false, "euro_rooms_range": null, "price": 18000, "price_includes_utilities": false, "deposit": null, "deposit_negotiable": null, "area": null, "floor": null, "floors_total": null, "address": null, "complex": null, "district_hint": "Пестречинский", "district_candidates": null, "building_type": "новостройка", "owner_type": null, "has_keys": null, "condition": null, "available_until": null, "commission_percent": null, "kickback_percent": null, "commission_shared": null, "tenant_requirements": "БЖ", "description": null, "contact": "89XXXXXXXXX"}

Пример 10 (городской ориентир без адреса/ЖК → hint из закрытого списка).
Текст: "Сдаю 2к у Чаши, рядом Меридиан, 40+ку, упак, наших. 89XXXXXXXXX".
JSON: {"is_listing": true, "property_type": "apartment", "rooms": 2, "euro_format": false, "euro_rooms_range": null, "price": 40000, "price_includes_utilities": false, "deposit": null, "deposit_negotiable": null, "area": null, "floor": null, "floors_total": null, "address": null, "complex": null, "district_hint": "Ново-Савиновский", "district_candidates": null, "building_type": null, "owner_type": null, "has_keys": null, "condition": "упак", "available_until": null, "commission_percent": null, "kickback_percent": null, "commission_shared": null, "tenant_requirements": "наших", "description": null, "contact": "89XXXXXXXXX"}

Пример 11 (составное имя ЖК + улица в названии — НЕ обрезать; есть и ЖК и адрес → hint=null).
Текст: "Сдаю 2к 40+ку, *ЖК Каскад Амирхана 12 Е* 70 кв.м, БЖ, не берут иностранцев. 89XXXXXXXXX Владимир".
JSON: {"is_listing": true, "property_type": "apartment", "rooms": 2, "euro_format": false, "euro_rooms_range": null, "price": 40000, "price_includes_utilities": false, "deposit": null, "deposit_negotiable": null, "area": 70, "floor": null, "floors_total": null, "address": "Амирхана 12", "complex": "Каскад Амирхана", "district_hint": null, "district_candidates": null, "building_type": null, "owner_type": null, "has_keys": null, "condition": null, "available_until": null, "commission_percent": null, "kickback_percent": null, "commission_shared": null, "tenant_requirements": "БЖ, не берут иностранцев", "description": "корпус Е", "contact": "Владимир 89XXXXXXXXX"}

Пример 12 (латиница в имени ЖК → транслитерация; есть ЖК → hint=null).
Текст: "Сдам 1к в Art City, 35м², 8/16, 30+ку, упак. 89XXXXXXXXX".
JSON: {"is_listing": true, "property_type": "apartment", "rooms": 1, "euro_format": false, "euro_rooms_range": null, "price": 30000, "price_includes_utilities": false, "deposit": null, "deposit_negotiable": null, "area": 35, "floor": 8, "floors_total": 16, "address": null, "complex": "Арт Сити", "district_hint": null, "district_candidates": null, "building_type": null, "owner_type": null, "has_keys": null, "condition": "упак", "available_until": null, "commission_percent": null, "kickback_percent": null, "commission_shared": null, "tenant_requirements": null, "description": null, "contact": "89XXXXXXXXX"}

Пример 13 (перекрёсток БЕЗ номера дома — берём первую улицу как имя без слеша).
Текст: "2к полноценная, ул Гвардейская/Аделя Кутуя, 40 м², 45+ку для 2 чел, 50+ку для 3 чел, залог 100%".
JSON: {"is_listing": true, "property_type": "apartment", "rooms": 2, "euro_format": false, "euro_rooms_range": null, "price": 45000, "price_includes_utilities": false, "deposit": null, "deposit_negotiable": null, "area": 40, "floor": null, "floors_total": null, "address": "Гвардейская", "complex": null, "district_hint": null, "district_candidates": null, "building_type": null, "owner_type": null, "has_keys": null, "condition": null, "available_until": null, "commission_percent": null, "kickback_percent": null, "commission_shared": null, "tenant_requirements": null, "description": null, "contact": null}

Пример 14 (казанский сленг "Nкк" — двойное «к» — это N-комнатная квартира; нет слова "квартира" в тексте, и это нормально).
Текст: "*Ново-Савиновский район*\n2кк\nул. Четаева 44\n80000 + ку\n40000 залог делимый\n60 кВм\n5/10 этаж".
JSON: {"is_listing": true, "property_type": "apartment", "rooms": 2, "euro_format": false, "euro_rooms_range": null, "price": 80000, "price_includes_utilities": false, "deposit": 40000, "deposit_negotiable": true, "area": 60, "floor": 5, "floors_total": 10, "address": "Четаева 44", "complex": null, "district_hint": null, "district_candidates": null, "building_type": null, "owner_type": null, "has_keys": null, "condition": null, "available_until": null, "commission_percent": null, "kickback_percent": null, "commission_shared": null, "tenant_requirements": null, "description": null, "contact": null}
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
