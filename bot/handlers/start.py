from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import CommandStart
from database.db import get_or_create_user, get_user, get_clients, get_monitored_chats, set_work_mode
from bot.keyboards.menus import main_menu, bottom_menu, clients_menu, chats_menu
from config import OWNER_ID

router = Router()


def is_owner(telegram_id: int) -> bool:
    return telegram_id == OWNER_ID


@router.message(CommandStart())
async def cmd_start(message: Message):
    if not is_owner(message.from_user.id):
        await message.answer("⛔ Нет доступа.")
        return

    user = await get_or_create_user(
        telegram_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
    )

    status = "🟢 Мониторинг активен" if user.is_working else "⭕ Мониторинг выключен"
    auth_status = "✅ Аккаунт подключён" if user.is_authorized else "⚠️ Аккаунт не подключён (используй /auth)"

    await message.answer(
        f"👋 Привет, {message.from_user.first_name}!\n\n"
        f"📊 <b>ChatScanner</b> — мониторинг чатов риелторов\n\n"
        f"Статус: {status}\n"
        f"Аккаунт: {auth_status}\n\n"
        f"Выбери действие:",
        parse_mode="HTML",
        reply_markup=bottom_menu(user.is_working),
    )
    await message.answer("Меню:", reply_markup=main_menu(user.is_working))


@router.callback_query(F.data == "main_menu")
async def cb_main_menu(call: CallbackQuery):
    user = await get_user(call.from_user.id)
    if not user:
        return
    status = "🟢 Мониторинг активен" if user.is_working else "⭕ Мониторинг выключен"
    auth_status = "✅ Аккаунт подключён" if user.is_authorized else "⚠️ Аккаунт не подключён (/auth)"
    await call.message.edit_text(
        f"📊 <b>ChatScanner</b>\n\n"
        f"Статус: {status}\n"
        f"Аккаунт: {auth_status}\n\n"
        f"Выбери действие:",
        parse_mode="HTML",
        reply_markup=main_menu(user.is_working),
    )


# ── Bottom reply-keyboard handlers ───────────────────────────────────

@router.message(F.text.in_({"🟢 Начать мониторинг", "🔴 Стоп мониторинг"}))
async def btn_toggle_work(message: Message):
    if not is_owner(message.from_user.id):
        return
    from userbot.scanner import scanner
    from bot.handlers.work_mode import _start_monitoring

    user = await get_user(message.from_user.id)
    if not user:
        return
    if not user.is_authorized:
        await message.answer("⚠️ Сначала подключи аккаунт: /auth")
        return

    new_state = not user.is_working
    await set_work_mode(message.from_user.id, new_state)

    if new_state:
        await _start_monitoring(message.from_user.id)
        status_text = "🟢 <b>Мониторинг запущен!</b>"
    else:
        await scanner.stop_listening(message.from_user.id)
        status_text = "🔴 <b>Мониторинг остановлен.</b>"

    await message.answer(status_text, parse_mode="HTML", reply_markup=bottom_menu(new_state))
    await message.answer("Меню:", reply_markup=main_menu(new_state))


@router.message(F.text == "👥 Мои клиенты")
async def btn_clients(message: Message):
    if not is_owner(message.from_user.id):
        return
    user = await get_user(message.from_user.id)
    if not user:
        return
    client_list = await get_clients(user.id)
    await message.answer("👥 <b>Мои клиенты</b>", parse_mode="HTML", reply_markup=clients_menu(client_list))


@router.message(F.text == "📡 Выбор чатов")
async def btn_chats(message: Message):
    if not is_owner(message.from_user.id):
        return
    user = await get_user(message.from_user.id)
    if not user:
        return
    monitored = await get_monitored_chats(user.id)
    await message.answer("📡 <b>Мониторинг чатов</b>", parse_mode="HTML", reply_markup=chats_menu(monitored))
