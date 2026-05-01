"""Work mode toggle and the core scanning/matching logic.

Pipeline (v4 architecture):
  1. Pre-filter regex (drop obvious non-rentals)
  2. Haiku 4.5 extraction with content-hash cache
  3. Geocoder cascade (LLM hint → 2GIS Places → Geocoder → street_buildings)
  4. Hard filters (type/rooms/price/district with multi-district intersection)
  5. Semantic Haiku match (covers tenant restrictions + ЖК priorities + freeform)
  6. Save match (with multi-district flag for UI annotation)
"""
import hashlib
import logging
import asyncio
import re
import time
from aiogram import Router, F, Bot
from aiogram.types import CallbackQuery

from database.db import (
    get_user, set_work_mode, get_clients, get_monitored_chats, save_match,
    is_duplicate_match,
)
from bot.keyboards.menus import main_menu
from userbot.scanner import scanner
from ai.extractor import extract_listing
from ai.geocoder import resolve_district
from ai.matcher import hard_filters, semantic_match
from config import PROPERTY_TYPES

router = Router()
logger = logging.getLogger(__name__)

_bot: Bot = None

# Cache by content hash: same text reposted to N chats = 1 LLM call.
_listing_cache: dict[str, dict] = {}
_listing_locks: dict[str, asyncio.Lock] = {}
_CACHE_TTL = 1800  # 30 minutes

# Per-content serialization for the *full* handler (extract → match → save).
# Repost-bots fan the same listing into N chats within milliseconds; without
# this lock, all N invocations race past is_duplicate_match before any of
# them commits a Match row, so the dedupe DB check finds nothing and every
# repost saves its own copy.
_process_locks: dict[str, asyncio.Lock] = {}

_SKIP_RE = re.compile(
    r"\b(продаж[аеу]|продаётс?я|продам|куп[лию]|покупк[аеу]|"
    r"коттедж|таунхаус|дач[аеу]|участок|участки|"
    r"коммерческ|нежилое|офис[ыа]?|склад|магазин|"
    r"ищу\s+(?:квартир|комнат|сним))",
    re.IGNORECASE,
)


def _content_key(text: str) -> str:
    norm = re.sub(r"\s+", " ", text).strip().lower()
    return hashlib.md5(norm.encode("utf-8")).hexdigest()


async def _get_listing(text: str) -> dict:
    """Extract + geocode once per unique text, cached for 30min."""
    if _SKIP_RE.search(text):
        return {"is_listing": False}

    key = _content_key(text)

    cached = _listing_cache.get(key)
    if cached and time.monotonic() - cached["ts"] < _CACHE_TTL:
        return cached["listing"]

    if key not in _listing_locks:
        _listing_locks[key] = asyncio.Lock()
    lock = _listing_locks[key]

    async with lock:
        cached = _listing_cache.get(key)
        if cached and time.monotonic() - cached["ts"] < _CACHE_TTL:
            return cached["listing"]

        listing = await extract_listing(text)
        if listing.get("is_listing"):
            listing = await resolve_district(listing)

        _listing_cache[key] = {"listing": listing, "ts": time.monotonic()}

        # Lazy GC of stale entries
        now = time.monotonic()
        stale = [k for k, v in _listing_cache.items() if now - v["ts"] > _CACHE_TTL]
        for k in stale:
            _listing_cache.pop(k, None)
            _listing_locks.pop(k, None)
            _process_locks.pop(k, None)

    return listing


def set_bot(bot: Bot):
    global _bot
    _bot = bot


# ── Toggle Work Mode ──────────────────────────────────────────────────

@router.callback_query(F.data == "toggle_work")
async def cb_toggle_work(call: CallbackQuery):
    user = await get_user(call.from_user.id)
    if not user:
        return

    if not user.is_authorized:
        await call.answer("⚠️ Сначала подключи аккаунт: /auth", show_alert=True)
        return

    new_state = not user.is_working
    await set_work_mode(call.from_user.id, new_state)

    if new_state:
        await _start_monitoring(call.from_user.id)
        status_text = "🟢 <b>Мониторинг запущен!</b>\n\nЯ буду присылать уведомления о подходящих объектах."
    else:
        await scanner.stop_listening(call.from_user.id)
        status_text = "🔴 <b>Мониторинг остановлен.</b>"

    await call.message.edit_text(
        status_text,
        parse_mode="HTML",
        reply_markup=main_menu(new_state),
    )


async def _start_monitoring(telegram_id: int):
    user = await get_user(telegram_id)
    if not user:
        return

    chats = await get_monitored_chats(user.id)
    chat_ids = [c.chat_id for c in chats]

    if not chat_ids:
        logger.warning(f"User {telegram_id} started work mode but has no monitored chats")
        return

    tg_client = scanner._clients.get(telegram_id)
    if not tg_client or not tg_client.is_connected():
        logger.error(f"UserBot for {telegram_id} not connected")
        return

    await scanner.start_listening(telegram_id, chat_ids)
    logger.info(f"Started monitoring {len(chat_ids)} chats for user {telegram_id}")


# ── Message Processing ─────────────────────────────────────────────

async def process_new_message(telegram_id: int, chat_id: int, chat_name: str, event):
    """Called by the userbot scanner for each new message in monitored chats."""
    global _bot
    if not _bot:
        return

    user = await get_user(telegram_id)
    if not user or not user.is_working:
        return

    message = event.message
    # Telethon Message: .text already covers media captions; .caption doesn't exist on Telethon objects.
    text = (getattr(message, "text", None) or getattr(message, "message", None) or "")
    if len(text.strip()) < 30:
        return

    logger.info(f"📨 [{chat_name}]: {text[:100]}")

    listing = await _get_listing(text)
    logger.info(
        f"🤖 Haiku: is_listing={listing.get('is_listing')} type={listing.get('property_type')} "
        f"price={listing.get('price')}"
    )
    if not listing.get("is_listing"):
        return

    src = listing.get("district_source") or "—"
    multi_tag = " [MULTI]" if listing.get("district_multi") else ""
    logger.info(
        f"📍 Район: {listing.get('district')}{multi_tag} (via={src}, ЖК={listing.get('complex')})"
    )

    # Serialize the per-listing pipeline so concurrent reposts of the same text
    # (different chats, same content_key) don't race past is_duplicate_match.
    process_key = _content_key(text)
    proc_lock = _process_locks.setdefault(process_key, asyncio.Lock())

    async with proc_lock:
        clients = await get_clients(user.id, active_only=True)
        for client in clients:
            passes, reason, score = hard_filters(listing, client)
            if not passes:
                logger.debug(f"  ❌ {client.name}: {reason}")
                continue

            # Dedupe BEFORE the LLM call: same listing reposted across chats must not
            # trigger another semantic_match — that's wasted spend on a guaranteed dup.
            if await is_duplicate_match(user.id, client.id, chat_id, message.id, text, listing):
                logger.info(f"  ⏭ Дубликат для {client.name}")
                continue

            sem_result = await semantic_match(listing, text, client)
            if not sem_result.get("matches"):
                logger.info(f"  🚫 {client.name}: {sem_result.get('reason')}")
                continue

            await save_match(
                user_id=user.id,
                client_id=client.id,
                chat_id=chat_id,
                chat_name=chat_name,
                message_id=message.id,
                message_text=text[:2000],
                extracted_data=listing,
                match_score=score,
            )
            logger.info(f"  💾 Сохранён матч для {client.name} (score={score}%)")


# ── Notification builder ────────────────────────────────────────────

def _build_notification(client, listing: dict, chat_name: str, score: int, raw_text: str) -> str:
    """Render the match card. Adds multi-district warning when applicable."""
    prop_type = PROPERTY_TYPES.get(listing.get("property_type", ""), listing.get("property_type", "—"))

    # Price
    price = listing.get("price")
    price_str = f"{int(price):,}".replace(",", " ") + " ₽" if price else "—"
    if price and listing.get("price_includes_utilities") is True:
        price_str += " (всё вкл.)"
    elif price and listing.get("price_includes_utilities") is False:
        price_str += " + КУ"

    # Deposit
    deposit = listing.get("deposit")
    if deposit is not None and deposit > 0:
        dep_str = f"{int(deposit):,}".replace(",", " ") + " ₽"
        if listing.get("deposit_negotiable"):
            dep_str += " (делимый)"
    elif deposit == 0:
        dep_str = "нет залога"
    else:
        dep_str = None

    area = listing.get("area")
    area_str = f"{area} м²" if area else "—"

    rooms = listing.get("rooms")
    euro = listing.get("euro_format")
    prop_t = listing.get("property_type")
    if prop_t == "room":
        rooms_str = "Комната"
    elif rooms:
        rooms_str = f"{rooms}-комн." + (" (евро)" if euro else "")
    else:
        rooms_str = "—"

    floor = listing.get("floor")
    floors_total = listing.get("floors_total")
    if floor and floors_total:
        floor_str = f"{floor}/{floors_total} эт."
    elif floor:
        floor_str = f"{floor} эт."
    else:
        floor_str = None

    district = listing.get("district") or "—"
    address = listing.get("address") or "—"
    complex_name = listing.get("complex")

    owner_type = listing.get("owner_type")
    owner_str = "Собственник" if owner_type == "owner" else ("Агент" if owner_type == "agent" else None)
    has_keys = listing.get("has_keys")

    condition = listing.get("condition")
    building_type = listing.get("building_type")
    available_until = listing.get("available_until")

    commission_percent = listing.get("commission_percent")
    kickback_percent = listing.get("kickback_percent")
    commission_shared = listing.get("commission_shared")

    tenant_req = listing.get("tenant_requirements")

    contact = listing.get("contact") or "—"
    description = listing.get("description") or ""

    score_stars = "⭐" * (score // 20) if score else ""

    lines = [
        f"🔔 <b>Объект для клиента: {client.name}</b>",
        f"Совпадение: {score}% {score_stars}",
        "",
        f"📌 Чат: {chat_name}",
        "",
        f"🏠 Аренда | {prop_type}",
        f"🚪 {rooms_str}",
        f"📐 Площадь: {area_str}",
    ]
    if floor_str:
        lines.append(f"🏗 Этаж: {floor_str}")
    lines.append(f"💰 Цена: {price_str}")
    if dep_str:
        lines.append(f"🔒 Залог: {dep_str}")

    comm_parts = []
    if commission_percent is not None:
        comm_parts.append(f"комиссия {commission_percent}%")
    if kickback_percent is not None:
        comm_parts.append(f"откат {kickback_percent}%")
    elif commission_shared:
        comm_parts.append("С+")
    if comm_parts:
        lines.append(f"💼 {', '.join(comm_parts)}")

    lines.append(f"🗺 Район: {district}")
    # Multi-district annotation: "осторожнее, улица идёт через X и Y"
    if listing.get("district_multi"):
        all_districts = listing.get("districts_all") or []
        if len(all_districts) > 1:
            joined = " и ".join(all_districts)
            lines.append(f"⚠️ <i>Осторожнее: улица идёт через районы {joined}, дом не указан — уточни у владельца</i>")
        else:
            lines.append("⚠️ <i>Район определён по нескольким зданиям, возможна погрешность — уточни у владельца</i>")

    lines.append(f"📍 Адрес: {address}")
    if complex_name:
        lines.append(f"🏢 ЖК: {complex_name}")

    details = []
    if owner_str:
        details.append(owner_str)
    if has_keys:
        details.append("ключи есть")
    if condition:
        details.append(condition)
    if building_type:
        details.append(building_type)
    if available_until:
        details.append(f"до: {available_until}")
    if details:
        lines.append(f"🔑 {' · '.join(details)}")

    if tenant_req:
        lines.append(f"👥 Жильцы: {tenant_req}")
    lines.append(f"📞 Контакт: {contact}")
    if description:
        lines += ["", f"📝 {description[:300]}"]
    lines += ["", "— — — — — — — — — —", "<i>Исходное сообщение:</i>", f"<code>{raw_text[:500]}</code>"]
    return "\n".join(lines)
