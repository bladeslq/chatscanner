import re
import logging

from aiogram import Router, Bot, F
from aiogram.types import (
    Message,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from telethon.errors import (
    SessionPasswordNeededError,
    PasswordHashInvalidError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    PhoneNumberInvalidError,
    FloodWaitError,
)

from database.db import get_user, update_user
from userbot.scanner import scanner
from bot.keyboards.menus import bottom_menu

router = Router()
logger = logging.getLogger(__name__)


class AuthStates(StatesGroup):
    waiting_phone = State()
    waiting_code = State()
    waiting_2fa = State()


def _phone_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Отправить мой номер", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


@router.callback_query(F.data == "start_auth")
async def cb_start_auth(call, state: FSMContext):
    await call.message.delete()
    await _start_phone_auth(call.message, state)
    await call.answer()


@router.message(Command("auth"))
async def cmd_auth(message: Message, state: FSMContext):
    try:
        await message.delete()
    except Exception:
        pass
    user = await get_user(message.from_user.id)
    if user and user.is_authorized and user.session_string:
        await message.answer("✅ Аккаунт уже подключён.\n\nЧтобы переподключить — /reauth")
        return
    await _start_phone_auth(message, state)


@router.message(Command("reauth"))
async def cmd_reauth(message: Message, state: FSMContext):
    try:
        await message.delete()
    except Exception:
        pass
    await update_user(message.from_user.id, is_authorized=False, session_string=None)
    await _start_phone_auth(message, state)


async def _start_phone_auth(message: Message, state: FSMContext):
    old_data = await state.get_data()
    for msg_id in old_data.get("auth_msgs", []):
        try:
            await message.bot.delete_message(message.chat.id, msg_id)
        except Exception:
            pass
    await state.clear()
    await state.set_state(AuthStates.waiting_phone)
    msg = await message.answer(
        "📱 <b>Подключение аккаунта</b>\n\n"
        "Нажми кнопку ниже или введи номер вручную в формате <code>+79991234567</code>.",
        parse_mode="HTML",
        reply_markup=_phone_keyboard(),
    )
    await state.update_data(auth_msgs=[msg.message_id])


@router.message(AuthStates.waiting_phone, F.contact)
async def got_contact(message: Message, state: FSMContext):
    if message.contact.user_id != message.from_user.id:
        await message.answer("❌ Отправь свой собственный контакт.")
        return
    phone = message.contact.phone_number
    if not phone.startswith("+"):
        phone = "+" + phone
    try:
        await message.delete()
    except Exception:
        pass
    await _request_code(message, state, phone)


@router.message(AuthStates.waiting_phone, F.text)
async def got_phone_text(message: Message, state: FSMContext):
    digits = _digits(message.text.strip())
    if len(digits) < 10:
        await message.answer("❌ Не похоже на номер. Введи в формате +79991234567.")
        return
    try:
        await message.delete()
    except Exception:
        pass
    await _request_code(message, state, "+" + digits)


async def _request_code(message: Message, state: FSMContext, phone: str):
    status = await message.answer("⏳ Отправляю код...", reply_markup=ReplyKeyboardRemove())

    tg_client = await scanner.create_client(message.from_user.id)
    err: str | None = None
    sent = None
    try:
        await tg_client.connect()
        sent = await tg_client.send_code_request(phone)
    except PhoneNumberInvalidError:
        err = "❌ Неверный номер. /auth — попробуй снова."
    except FloodWaitError as e:
        err = f"❌ Слишком много попыток. Подожди {e.seconds} сек."
    except Exception as e:
        logger.exception("send_code_request failed")
        err = f"❌ Ошибка: {e}\n\n/auth — попробуй снова."

    try:
        await status.delete()
    except Exception:
        pass

    if err is not None:
        await message.answer(err)
        await state.clear()
        return

    data = await state.get_data()
    auth_msgs = data.get("auth_msgs", [])
    code_msg = await message.answer(
        "📨 Код отправлен. Введи его через пробел:\n\n"
        "<code>1 2 3 4 5</code>",
        parse_mode="HTML",
    )
    auth_msgs.append(code_msg.message_id)
    await state.update_data(phone=phone, phone_code_hash=sent.phone_code_hash, auth_msgs=auth_msgs)
    await state.set_state(AuthStates.waiting_code)


@router.message(AuthStates.waiting_code)
async def got_code(message: Message, state: FSMContext, bot: Bot):
    code = _digits(message.text)
    if len(code) < 4:
        await message.answer("❌ Слишком короткий код. Попробуй ещё раз:")
        return

    data = await state.get_data()
    phone = data.get("phone")
    phone_code_hash = data.get("phone_code_hash")
    tg_client = await scanner.get_client(message.from_user.id)
    if not tg_client or not phone or not phone_code_hash:
        await message.answer("❌ Сессия потеряна. Начни заново — /auth")
        await state.clear()
        return

    try:
        await tg_client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
    except SessionPasswordNeededError:
        try:
            await message.delete()
        except Exception:
            pass
        data = await state.get_data()
        auth_msgs = data.get("auth_msgs", [])
        twofa_msg = await message.answer("🔐 Введи облачный пароль Telegram (2FA):")
        auth_msgs.append(twofa_msg.message_id)
        await state.update_data(auth_msgs=auth_msgs)
        await state.set_state(AuthStates.waiting_2fa)
        return
    except PhoneCodeInvalidError:
        await message.answer("❌ Неверный код. Попробуй ещё раз:")
        return
    except PhoneCodeExpiredError:
        await message.answer("❌ Код истёк. Начни заново — /auth")
        await state.clear()
        return
    except Exception as e:
        logger.exception("sign_in with code failed")
        await message.answer(f"❌ Ошибка: {e}\n\n/auth — попробуй снова.")
        await state.clear()
        return

    try:
        await message.delete()
    except Exception:
        pass
    auth_msgs = (await state.get_data()).get("auth_msgs", [])
    await state.clear()
    await _finish_auth(message.from_user.id, tg_client, bot, auth_msgs)


@router.message(AuthStates.waiting_2fa)
async def got_2fa(message: Message, state: FSMContext, bot: Bot):
    password = message.text.strip()
    tg_client = await scanner.get_client(message.from_user.id)
    if not tg_client:
        await message.answer("❌ Сессия потеряна. Начни заново — /auth")
        await state.clear()
        return

    try:
        await tg_client.sign_in(password=password)
    except PasswordHashInvalidError:
        await message.answer("❌ Неверный пароль. Попробуй ещё раз:")
        return
    except Exception as e:
        logger.exception("sign_in with password failed")
        await message.answer(f"❌ Ошибка: {e}\n\n/auth — попробуй снова.")
        await state.clear()
        return

    try:
        await message.delete()
    except Exception:
        pass
    auth_msgs = (await state.get_data()).get("auth_msgs", [])
    await state.clear()
    await _finish_auth(message.from_user.id, tg_client, bot, auth_msgs)


async def _finish_auth(telegram_id: int, tg_client, bot: Bot, auth_msgs: list = None):
    for msg_id in (auth_msgs or []):
        try:
            await bot.delete_message(telegram_id, msg_id)
        except Exception:
            pass
    try:
        session_string = tg_client.session.save()
        me = await tg_client.get_me()
        await update_user(
            telegram_id,
            session_string=session_string,
            is_authorized=True,
            phone=str(me.phone) if me.phone else None,
        )
        from bot.handlers.start import set_main_msg
        msg = await bot.send_message(
            telegram_id,
            f"Аккаунт подключён!\n\n"
            f"{me.first_name} (@{me.username})\n\n"
            f"Теперь добавь чаты через меню.",
            parse_mode="HTML",
            reply_markup=bottom_menu(False),
        )
        set_main_msg(telegram_id, msg.message_id)
        logger.info(f"User {telegram_id} authorized as @{me.username}")
    except Exception as e:
        logger.exception("finish_auth failed")
        await bot.send_message(telegram_id, f"❌ Ошибка сохранения сессии: {e}")
