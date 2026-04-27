"""Work mode toggle and the core scanning/matching logic."""
import logging
from aiogram import Router, F, Bot
from aiogram.types import CallbackQuery

from database.db import (
    get_user, set_work_mode, get_clients, get_monitored_chats, save_match,
)
from bot.keyboards.menus import main_menu, bottom_menu
from userbot.scanner import scanner
from ai.grok import extract_listing, check_match
from ai.dadata import enrich_district
from config import PROPERTY_TYPES, TRANSACTION_TYPES

router = Router()
logger = logging.getLogger(__name__)

# Global bot reference set from main.py
_bot: Bot = None


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
    await call.message.answer(" ", reply_markup=bottom_menu(new_state))


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

    # Extract listing data with Grok
    listing = await extract_listing(text)
    logger.info(f"🤖 Groq ответ: is_listing={listing.get('is_listing')} type={listing.get('property_type')} price={listing.get('price')}")
    if not listing.get("is_listing"):
        return

    listing = await enrich_district(listing)
    logger.info(f"📍 Район итого: {listing.get('district')} (ЖК: {listing.get('complex')})")

    logger.info(f"✅ Объект найден в [{chat_name}]: {listing.get('property_type')} {listing.get('price')}")

    # Check against each active client
    clients = await get_clients(user.id, active_only=True)
    for client in clients:
        matches, score = check_match(listing, client)
        if not matches:
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

        try:
            notification = _build_notification(client, listing, chat_name, score, text)
            await _bot.send_message(telegram_id, notification, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Ошибка отправки уведомления для {telegram_id}: {e}")


def _build_notification(client, listing: dict, chat_name: str, score: int, raw_text: str) -> str:
    prop_type = PROPERTY_TYPES.get(listing.get("property_type", ""), listing.get("property_type", "—"))
    tr_type = TRANSACTION_TYPES.get(listing.get("transaction_type", ""), listing.get("transaction_type", "—"))

    price = listing.get("price")
    price_str = f"{int(price):,}".replace(",", " ") + " ₽" if price else "—"
    if price and listing.get("price_includes_utilities") is True:
        price_str += " (всё вкл.)"
    elif price and listing.get("price_includes_utilities") is False:
        price_str += " + КУ"

    deposit = listing.get("deposit")
    if deposit:
        dep_str = f"{int(deposit):,}".replace(",", " ") + " ₽"
        if listing.get("deposit_negotiable"):
            dep_str += " (делимый)"
    else:
        dep_str = None

    area = listing.get("area")
    area_str = f"{area} м²" if area else "—"

    rooms = listing.get("rooms")
    euro = listing.get("euro_format")
    if rooms:
        rooms_str = f"{rooms}-комн." + (" (евро)" if euro else "")
    else:
        rooms_str = "—"

    district = listing.get("district") or "—"
    address = listing.get("address") or "—"
    complex_name = listing.get("complex")
    contact = listing.get("contact") or "—"
    description = listing.get("description") or ""
    tenant_req = listing.get("tenant_requirements")

    score_stars = "⭐" * (score // 20) if score else ""

    lines = [
        f"🔔 <b>Объект для клиента: {client.name}</b>",
        f"Совпадение: {score}% {score_stars}",
        f"",
        f"📌 Чат: {chat_name}",
        f"",
        f"🏠 {tr_type} | {prop_type}",
        f"🚪 Комнат: {rooms_str}",
        f"📐 Площадь: {area_str}",
        f"💰 Цена: {price_str}",
    ]
    if dep_str:
        lines.append(f"🔒 Залог: {dep_str}")
    lines += [
        f"🗺 Район: {district}",
        f"📍 Адрес: {address}",
    ]
    if complex_name:
        lines.append(f"🏢 ЖК: {complex_name}")
    if tenant_req:
        lines.append(f"👥 Жильцы: {tenant_req}")
    lines.append(f"📞 Контакт: {contact}")
    if description:
        lines += ["", f"📝 {description[:300]}"]
    lines += ["", "— — — — — — — — — —", f"<i>Исходное сообщение:</i>", f"<code>{raw_text[:500]}</code>"]
    return "\n".join(lines)
