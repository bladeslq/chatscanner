"""Work mode toggle and the core scanning/matching logic."""
import logging
import asyncio
import time
from aiogram import Router, F, Bot
from aiogram.types import CallbackQuery

from database.db import (
    get_user, set_work_mode, get_clients, get_monitored_chats, save_match,
    is_duplicate_match,
)
from bot.keyboards.menus import main_menu
from userbot.scanner import scanner
from ai.grok import extract_listing, check_match, check_tenant_conflict
from ai.dadata import enrich_district  # used inside _get_listing
from config import PROPERTY_TYPES, TRANSACTION_TYPES

router = Router()
logger = logging.getLogger(__name__)

# Global bot reference set from main.py
_bot: Bot = None

# Cache: (chat_id, message_id) → {listing, ts}
# Prevents parsing the same message multiple times when several users monitor the same chat
_listing_cache: dict[tuple, dict] = {}
_listing_locks: dict[tuple, asyncio.Lock] = {}
_CACHE_TTL = 300  # 5 minutes


async def _get_listing(chat_id: int, message_id: int, text: str) -> dict:
    key = (chat_id, message_id)

    # Fast path — already cached
    cached = _listing_cache.get(key)
    if cached and time.monotonic() - cached["ts"] < _CACHE_TTL:
        return cached["listing"]

    # Get or create a lock for this specific message
    if key not in _listing_locks:
        _listing_locks[key] = asyncio.Lock()
    lock = _listing_locks[key]

    async with lock:
        # Double-check after acquiring lock (another coroutine may have just filled it)
        cached = _listing_cache.get(key)
        if cached and time.monotonic() - cached["ts"] < _CACHE_TTL:
            return cached["listing"]

        listing = await extract_listing(text)
        if listing.get("is_listing"):
            listing = await enrich_district(listing)

        _listing_cache[key] = {"listing": listing, "ts": time.monotonic()}

        # Cleanup stale entries to avoid memory leak
        now = time.monotonic()
        stale = [k for k, v in _listing_cache.items() if now - v["ts"] > _CACHE_TTL]
        for k in stale:
            _listing_cache.pop(k, None)
            _listing_locks.pop(k, None)

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


# ── Message Processing (called by scanner) ───────────────────────────

async def process_new_message(telegram_id: int, chat_id: int, chat_name: str, event):
    """Called by the userbot scanner for each new message in monitored chats."""
    global _bot
    if not _bot:
        return

    user = await get_user(telegram_id)
    if not user or not user.is_working:
        return

    message = event.message
    text = message.text or message.caption or ""
    if len(text.strip()) < 30:
        return  # too short to be a listing

    logger.info(f"📨 Сообщение из [{chat_name}]: {text[:100]}")

    # Parse once per (chat_id, message_id) — cached for all users monitoring the same chat
    listing = await _get_listing(chat_id, message.id, text)
    logger.info(f"🤖 Groq ответ: is_listing={listing.get('is_listing')} type={listing.get('property_type')} price={listing.get('price')}")
    if not listing.get("is_listing"):
        return

    logger.info(f"📍 Район итого: {listing.get('district')} (ЖК: {listing.get('complex')})")

    logger.info(f"✅ Объект найден в [{chat_name}]: {listing.get('property_type')} {listing.get('price')}")

    # Check against each active client
    clients = await get_clients(user.id, active_only=True)
    for client in clients:
        matches, score = check_match(listing, client)
        if not matches:
            continue

        # second LLM call: tenant conflict check (only when both sides have data)
        if listing.get("tenant_requirements") and client.notes:
            conflict = await check_tenant_conflict(listing["tenant_requirements"], client.notes)
            if conflict:
                logger.info(f"🚫 Конфликт требований для клиента {client.name}, пропускаем")
                continue

        if await is_duplicate_match(user.id, client.id, chat_id, message.id, text, listing):
            logger.info(f"⏭ Дубликат, пропускаем для клиента {client.name}")
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
        logger.info(f"💾 Сохранён матч для клиента {client.name} (score={score}%)")


def _build_notification(client, listing: dict, chat_name: str, score: int, raw_text: str) -> str:
    prop_type = PROPERTY_TYPES.get(listing.get("property_type", ""), listing.get("property_type", "—"))
    tr_type = TRANSACTION_TYPES.get(listing.get("transaction_type", ""), listing.get("transaction_type", "—"))

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

    # Area
    area = listing.get("area")
    area_str = f"{area} м²" if area else "—"

    # Rooms
    rooms = listing.get("rooms")
    euro = listing.get("euro_format")
    prop_t = listing.get("property_type")
    if prop_t == "room":
        rooms_str = "Комната"
    elif rooms:
        rooms_str = f"{rooms}-комн." + (" (евро)" if euro else "")
    else:
        rooms_str = "—"

    # Floor
    floor = listing.get("floor")
    floors_total = listing.get("floors_total")
    if floor and floors_total:
        floor_str = f"{floor}/{floors_total} эт."
    elif floor:
        floor_str = f"{floor} эт."
    else:
        floor_str = None

    # Location
    district = listing.get("district") or "—"
    address = listing.get("address") or "—"
    complex_name = listing.get("complex")

    # Owner / keys
    owner_type = listing.get("owner_type")
    owner_str = "Собственник" if owner_type == "owner" else ("Агент" if owner_type == "agent" else None)
    has_keys = listing.get("has_keys")

    # Condition
    condition = listing.get("condition")
    building_type = listing.get("building_type")

    # Available until
    available_until = listing.get("available_until")

    # Commission / kickback
    commission_percent = listing.get("commission_percent")
    kickback_percent = listing.get("kickback_percent")
    commission_shared = listing.get("commission_shared")

    # Tenant requirements
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
        f"🏠 {tr_type} | {prop_type}",
        f"🚪 {rooms_str}",
        f"📐 Площадь: {area_str}",
    ]
    if floor_str:
        lines.append(f"🏗 Этаж: {floor_str}")
    lines.append(f"💰 Цена: {price_str}")
    if dep_str:
        lines.append(f"🔒 Залог: {dep_str}")

    # Commission line
    comm_parts = []
    if commission_percent is not None:
        comm_parts.append(f"комиссия {commission_percent}%")
    if kickback_percent is not None:
        comm_parts.append(f"откат {kickback_percent}%")
    elif commission_shared:
        comm_parts.append("С+")
    if comm_parts:
        lines.append(f"💼 {', '.join(comm_parts)}")

    lines += [
        f"🗺 Район: {district}",
        f"📍 Адрес: {address}",
    ]
    if complex_name:
        lines.append(f"🏢 ЖК: {complex_name}")

    # Object details line
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
