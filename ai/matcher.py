"""Matching: hard filters + semantic Haiku check.

Semantic check covers:
  - tenant requirements vs client profile (БЖ + кошка → conflict)
  - complex priorities ("в приоритете ЖК Мой Ритм" in client.notes)
  - any other freeform preference (этаж, ремонт, балкон, etc.)

Replaces ai.grok.check_match + ai.grok.check_tenant_conflict (two LLM calls)
with one semantic call per (listing, client) pair, only when needed.
"""
import asyncio
import json
import re
import logging
import httpx
from anthropic import AsyncAnthropic
from config import ANTHROPIC_API_KEY, HAIKU_MODEL
from database.models import Client

logger = logging.getLogger(__name__)

_http_client = httpx.AsyncClient(verify=False)
_client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY, http_client=_http_client)
_sem = asyncio.Semaphore(3)


# ── Hard filters (deterministic, no LLM) ─────────────────────────
def hard_filters(listing: dict, client: Client) -> tuple[bool, str | None, int]:
    """Returns (passes, reject_reason, score_0_100).

    Multi-district streets (district_multi=True): pass if ANY listing district
    intersects client's wanted districts.
    """
    if not listing.get("is_listing"):
        return False, "не объявление об аренде", 0

    score = 0
    total_criteria = 0

    # property_type
    if client.property_type:
        total_criteria += 20
        if listing.get("property_type") == client.property_type:
            score += 20
        else:
            return False, f"тип не совпал", 0

    # rooms
    if client.min_rooms or client.max_rooms:
        total_criteria += 15
        rooms = listing.get("rooms")
        euro_range = listing.get("euro_rooms_range")
        if rooms is None and not euro_range:
            return False, "комнаты не извлечены", 0
        if euro_range and len(euro_range) == 2:
            lo, hi = euro_range
            cmin = client.min_rooms or 0
            cmax = client.max_rooms or 99
            if cmax < lo or cmin > hi:
                return False, "комнаты вне диапазона (евро)", 0
        else:
            if client.min_rooms and rooms < client.min_rooms:
                return False, f"комнат {rooms} меньше min {client.min_rooms}", 0
            if client.max_rooms and rooms > client.max_rooms:
                return False, f"комнат {rooms} больше max {client.max_rooms}", 0
        score += 15

    # price (5% tolerance)
    if client.min_price or client.max_price:
        total_criteria += 20
        price = listing.get("price")
        if price is None:
            return False, "цена не извлечена", 0
        if client.min_price and price < client.min_price * 0.95:
            return False, f"цена {price} ниже {client.min_price}", 0
        if client.max_price and price > client.max_price * 1.05:
            return False, f"цена {price} выше {client.max_price}", 0
        score += 20

    # area (10% tolerance, soft — missing area passes through)
    if client.min_area or client.max_area:
        total_criteria += 15
        area = listing.get("area")
        if area is not None:
            if client.min_area and area < client.min_area * 0.9:
                return False, "площадь меньше min", 0
            if client.max_area and area > client.max_area * 1.1:
                return False, "площадь больше max", 0
            score += 15

    # district — strict, with multi-district intersection
    if client.districts:
        total_criteria += 10
        district = listing.get("district")
        candidates = listing.get("districts_all") or ([district] if district else [])
        if not candidates:
            return False, "район не определён", 0
        if not any(c in client.districts for c in candidates):
            return False, f"район {candidates} не пересекается с клиентским {client.districts}", 0
        score += 10

    final_score = int(score / total_criteria * 100) if total_criteria else 50
    return True, None, final_score


# ── Semantic match (single LLM call) ─────────────────────────────
SEMANTIC_SYSTEM = """Ты определяешь, подходит ли объявление об аренде квартиры конкретному клиенту с учётом его пожеланий и требований к жильцам в объявлении.

Возвращаешь СТРОГО JSON без markdown:
{
  "matches": true/false,
  "confidence": "high"/"medium"/"low",
  "reason": "краткое объяснение"
}

Правила:
- matches=false если в объявлении ЯВНО ЗАПРЕЩАЕТСЯ профиль клиента:
  • "БЖ" / "без животных" / "без питомцев" + клиент с животным → false
  • "БД" / "без детей" + клиент с ребёнком → false
  • "только наших" / "только РФ" + клиент-иностранец → false
  • "только девушку/парня/семью" если клиент не подходит
- matches=true если требования совпадают или нет ограничений
- "Можно ино" / "ино ок" + клиент-иностранец → matches=true
- "Можно с кошкой" / "питомцы ок" + клиент с кошкой → matches=true
- Если у клиента в notes указан приоритетный ЖК и объявление в этом ЖК → confidence=high
- Если приоритеты совпадают частично — confidence=medium
- Если требования не упомянуты — matches=true, confidence=high
- Если в notes клиента есть нестандартные пожелания (этаж, ремонт, балкон, тип дома) — учитывай их"""


# Markers that indicate the listing has tenant restrictions or special preferences
# worth running the semantic check. If neither side has anything — skip the call.
_NEEDS_SEMANTIC_RE = re.compile(
    r"\b(б[жд]\b|только\b|без\b|наших\b|нашу\b|ино\b|иностранц|"
    r"животн|питомц|кошк|собак|"
    r"детей|ребен|ребён|малыш|"
    r"девушк|девочк|парн|мужчин|семейн|сп\b|"
    r"снг\b|пожилы|студент|курящ)",
    re.IGNORECASE,
)


def _needs_semantic_check(listing: dict, client: Client) -> bool:
    """Skip the LLM call when neither side has constraints."""
    tenant_req = (listing.get("tenant_requirements") or "").strip()
    client_notes = (client.notes or "").strip()
    if not tenant_req and not client_notes:
        return False
    listing_text = (tenant_req + " " + (listing.get("description") or "")).lower()
    if not client_notes and not _NEEDS_SEMANTIC_RE.search(listing_text):
        return False
    return True


async def semantic_match(listing: dict, listing_text: str, client: Client) -> dict:
    """One Haiku call per (listing, client) pair.

    Returns: {matches: bool, confidence: 'high'|'medium'|'low', reason: str}
    """
    if not ANTHROPIC_API_KEY:
        return {"matches": True, "confidence": "low", "reason": "Anthropic key missing"}

    # Cheap path: skip when nothing to check
    if not _needs_semantic_check(listing, client):
        return {"matches": True, "confidence": "high", "reason": "нет требований к жильцам и пожеланий клиента"}

    user_msg = (
        f"Клиент: {client.name}\n"
        f"Заметки о клиенте: {client.notes or '(пусто)'}\n\n"
        f"Объявление (текст):\n{listing_text[:1500]}\n\n"
        f"Извлечённые требования к жильцам: {listing.get('tenant_requirements') or 'не указаны'}\n\n"
        "Подходит ли объявление этому клиенту? Верни JSON."
    )

    async with _sem:
        for attempt in range(2):
            try:
                msg = await _client.messages.create(
                    model=HAIKU_MODEL,
                    max_tokens=200,
                    system=[{"type": "text", "text": SEMANTIC_SYSTEM, "cache_control": {"type": "ephemeral"}}],
                    messages=[{"role": "user", "content": user_msg}],
                )
                raw = msg.content[0].text.strip()
                m = re.search(r"\{.*\}", raw, re.DOTALL)
                if not m:
                    return {"matches": True, "confidence": "low", "reason": "parse error"}
                try:
                    return json.loads(m.group())
                except json.JSONDecodeError:
                    return {"matches": True, "confidence": "low", "reason": "parse error"}
            except Exception as e:
                if attempt < 1:
                    await asyncio.sleep(2)
                    continue
                logger.warning(f"Semantic match error: {e}")
                # On error, do NOT block the listing — return matches=true, low confidence
                return {"matches": True, "confidence": "low", "reason": f"error: {e}"}
    return {"matches": True, "confidence": "low", "reason": "unknown"}
