from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import CommandStart
from database.db import get_or_create_user, get_user, get_clients, get_monitored_chats, set_work_mode, update_user
from bot.keyboards.menus import main_menu, bottom_menu, clients_menu, chats_menu

router = Router()

# Tracks the single "main" bot message per user (telegram_id -> message_id)
_main_msgs: dict[int, int] = {}


def is_owner(telegram_id: int) -> bool:
    """Kept for import compatibility with auth.py."""
    return True


def _welcome_text(user) -> str:
    monitoring = "✅" if user.is_working else "❌"
    auth = "✅" if user.is_authorized else "❌"
    return (
        f"<b>Привет, {user.first_name or 'риелтор'}!</b>\n\n"
        f"Статус мониторинга: {monitoring}\n"
        f"Статус аккаунта: {auth}"
    )


async def _edit_main(message: Message, text: str, reply_markup=None):
    """Edit the stored main message, or replace it with a new one if editing fails."""
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
            try:
                await message.bot.delete_message(tid, msg_id)
            except Exception:
                pass
    msg = await message.answer(text, parse_mode="HTML", reply_markup=reply_markup)
    _main_msgs[tid] = msg.message_id


async def _replace_main(message: Message, text: str, reply_markup=None):
    """Delete old main message and send a new one (needed when reply keyboard must change)."""
    tid = message.from_user.id
    old_id = _main_msgs.pop(tid, None)
    msg = await message.bot.send_message(tid, text, parse_mode="HTML", reply_markup=reply_markup)
    _main_msgs[tid] = msg.message_id
    if old_id:
        try:
            await message.bot.delete_message(tid, old_id)
        except Exception:
            pass


async def _delete_user_msg(message: Message):
    try:
        await message.delete()
    except Exception:
        pass


# ── /start ────────────────────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(message: Message):
    user = await get_or_create_user(
        telegram_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
    )

    await _delete_user_msg(message)

    if not user.is_authorized:
        from aiogram.utils.keyboard import InlineKeyboardBuilder
        kb = InlineKeyboardBuilder()
        kb.button(text="Авторизоваться", callback_data="start_auth")
        await message.answer(
            "Для работы необходимо подключить Telegram-аккаунт.",
            reply_markup=kb.as_markup(),
        )


# ── Inline "main_menu" callback (Назад from sub-menus) ───────────────

@router.callback_query(F.data == "main_menu")
async def cb_main_menu(call: CallbackQuery):
    user = await get_user(call.from_user.id)
    if not user:
        return
    await call.message.edit_text(
        _welcome_text(user),
        parse_mode="HTML",
        reply_markup=None,
    )
    _main_msgs[call.from_user.id] = call.message.message_id
    await call.answer()


# ── Bottom reply-keyboard handlers ───────────────────────────────────

@router.message(F.text.in_({"Начать мониторинг", "Стоп мониторинг"}))
async def btn_toggle_work(message: Message):
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

    from aiogram.utils.keyboard import InlineKeyboardBuilder

    kb_updater = await message.bot.send_message(
        message.from_user.id, "...", reply_markup=bottom_menu(new_state)
    )
    try:
        await message.bot.delete_message(message.from_user.id, kb_updater.message_id)
    except Exception:
        pass

    back_kb = InlineKeyboardBuilder()
    back_kb.button(text="Вернуться назад", callback_data="main_menu")
    await _replace_main(message, status_text, back_kb.as_markup())


@router.message(F.text == "Мои клиенты")
async def btn_clients(message: Message):
    await _delete_user_msg(message)
    user = await get_user(message.from_user.id)
    if not user:
        return
    client_list = await get_clients(user.id)
    await _edit_main(message, "<b>Мои клиенты</b>", clients_menu(client_list))


@router.message(F.text == "Выбор чатов")
async def btn_chats(message: Message):
    await _delete_user_msg(message)
    user = await get_user(message.from_user.id)
    if not user:
        return
    monitored = await get_monitored_chats(user.id)
    await _edit_main(message, "<b>Мониторинг чатов</b>", chats_menu(monitored))


@router.message(F.text == "Главная")
async def btn_home(message: Message):
    await _delete_user_msg(message)
    user = await get_user(message.from_user.id)
    if not user:
        return
    await _replace_main(message, _welcome_text(user), bottom_menu(user.is_working))


@router.message(F.text == "Выйти")
async def btn_logout(message: Message):
    await _delete_user_msg(message)

    user = await get_user(message.from_user.id)
    if not user or not user.is_authorized:
        await _edit_main(message, "Аккаунт не подключён.")
        return

    from userbot.scanner import scanner

    await set_work_mode(message.from_user.id, False)
    await scanner.stop_listening(message.from_user.id)

    try:
        client = scanner._clients.get(message.from_user.id)
        if client and client.is_connected():
            await client.log_out()
    except Exception:
        pass

    await update_user(message.from_user.id, is_authorized=False, session_string=None, is_working=False)

    await _edit_main(
        message,
        "Выход выполнен. Аккаунт отключён.\n\nДля подключения нового аккаунта используй /auth",
        None,
    )
