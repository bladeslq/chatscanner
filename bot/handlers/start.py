from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import CommandStart
from database.db import get_or_create_user, get_user, get_clients, get_monitored_chats, set_work_mode, update_user
from bot.keyboards.menus import main_menu, bottom_menu, clients_menu, chats_menu
from config import OWNER_ID

router = Router()

# Tracks the single "main" bot message per user (telegram_id -> message_id)
_main_msgs: dict[int, int] = {}


def is_owner(telegram_id: int) -> bool:
    return telegram_id == OWNER_ID


def _welcome_text(user) -> str:
    status = "Мониторинг активен" if user.is_working else "Мониторинг выключен"
    auth = "Аккаунт подключён" if user.is_authorized else "Аккаунт не подключён (используй /auth)"
    return f"<b>ChatScanner</b>\n\nСтатус: {status}\nАккаунт: {auth}"


async def _edit_main(message: Message, text: str, reply_markup=None):
    """Edit the stored main message, or send a new one if it's gone."""
    tid = message.from_user.id
    msg_id = _main_msgs.get(tid)
    if msg_id:
        try:
            await message.bot.edit_message_text(
                chat_id=tid,
                message_id=msg_id,
                text=text,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
            return
        except Exception:
            pass
    msg = await message.answer(text, parse_mode="HTML", reply_markup=reply_markup)
    _main_msgs[tid] = msg.message_id


async def _delete_user_msg(message: Message):
    try:
        await message.delete()
    except Exception:
        pass


# ── /start ────────────────────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(message: Message):
    if not is_owner(message.from_user.id):
        await message.answer("Нет доступа.")
        return

    user = await get_or_create_user(
        telegram_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
    )

    # /start itself is a user message — delete it to keep chat clean
    await _delete_user_msg(message)

    text = f"Привет, {user.first_name or 'риелтор'}!\n\n" + _welcome_text(user)
    tid = message.from_user.id
    msg_id = _main_msgs.get(tid)
    if msg_id:
        try:
            await message.bot.edit_message_text(
                chat_id=tid, message_id=msg_id,
                text=text, parse_mode="HTML",
                reply_markup=main_menu(user.is_working),
            )
            return
        except Exception:
            pass
    msg = await message.bot.send_message(
        tid, text, parse_mode="HTML",
        reply_markup=bottom_menu(user.is_working),
    )
    _main_msgs[tid] = msg.message_id


# ── Inline "main_menu" callback (Назад from sub-menus) ───────────────

@router.callback_query(F.data == "main_menu")
async def cb_main_menu(call: CallbackQuery):
    user = await get_user(call.from_user.id)
    if not user:
        return
    await call.message.edit_text(
        _welcome_text(user),
        parse_mode="HTML",
        reply_markup=main_menu(user.is_working),
    )
    _main_msgs[call.from_user.id] = call.message.message_id
    await call.answer()


# ── Bottom reply-keyboard handlers ───────────────────────────────────

@router.message(F.text.in_({"Начать мониторинг", "Стоп мониторинг"}))
async def btn_toggle_work(message: Message):
    if not is_owner(message.from_user.id):
        return
    await _delete_user_msg(message)

    from userbot.scanner import scanner
    from bot.handlers.work_mode import _start_monitoring

    user = await get_user(message.from_user.id)
    if not user:
        return
    if not user.is_authorized:
        await _edit_main(message, "Сначала подключи аккаунт: /auth")
        return

    new_state = not user.is_working
    await set_work_mode(message.from_user.id, new_state)

    if new_state:
        await _start_monitoring(message.from_user.id)
        status_text = "<b>Мониторинг запущен!</b>"
    else:
        await scanner.stop_listening(message.from_user.id)
        status_text = "<b>Мониторинг остановлен.</b>"

    # Send new message to update the reply keyboard button text (Начать ↔ Стоп)
    msg = await message.bot.send_message(
        message.from_user.id,
        status_text,
        parse_mode="HTML",
        reply_markup=bottom_menu(new_state),
    )
    _main_msgs[message.from_user.id] = msg.message_id


@router.message(F.text == "Мои клиенты")
async def btn_clients(message: Message):
    if not is_owner(message.from_user.id):
        return
    await _delete_user_msg(message)
    user = await get_user(message.from_user.id)
    if not user:
        return
    client_list = await get_clients(user.id)
    await _edit_main(message, "<b>Мои клиенты</b>", clients_menu(client_list))


@router.message(F.text == "Выбор чатов")
async def btn_chats(message: Message):
    if not is_owner(message.from_user.id):
        return
    await _delete_user_msg(message)
    user = await get_user(message.from_user.id)
    if not user:
        return
    monitored = await get_monitored_chats(user.id)
    await _edit_main(message, "<b>Мониторинг чатов</b>", chats_menu(monitored))


@router.message(F.text == "Главная")
async def btn_home(message: Message):
    if not is_owner(message.from_user.id):
        return
    await _delete_user_msg(message)
    user = await get_user(message.from_user.id)
    if not user:
        return
    await _edit_main(message, _welcome_text(user), main_menu(user.is_working))


@router.message(F.text == "Выйти")
async def btn_logout(message: Message):
    if not is_owner(message.from_user.id):
        return
    await _delete_user_msg(message)

    user = await get_user(message.from_user.id)
    if not user or not user.is_authorized:
        await _edit_main(message, "Аккаунт не подключён.")
        return

    from userbot.scanner import scanner

    # Stop monitoring first
    await set_work_mode(message.from_user.id, False)
    await scanner.stop_listening(message.from_user.id)

    # Log out Telethon session
    try:
        client = scanner._clients.get(message.from_user.id)
        if client and client.is_connected():
            await client.log_out()
    except Exception:
        pass

    # Clear session from DB
    await update_user(message.from_user.id, is_authorized=False, session_string=None, is_working=False)

    await _edit_main(
        message,
        "Выход выполнен. Аккаунт отключён.\n\nДля подключения нового аккаунта используй /auth",
        None,
    )
