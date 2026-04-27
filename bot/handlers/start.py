from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import CommandStart
from database.db import get_or_create_user, get_user
from bot.keyboards.menus import main_menu
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
        reply_markup=main_menu(user.is_working),
    )


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
