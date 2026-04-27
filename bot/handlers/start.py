from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import CommandStart
from database.db import get_or_create_user, get_user, get_clients, get_monitored_chats, set_work_mode
from bot.keyboards.menus import main_menu, bottom_menu, clients_menu, chats_menu

router = Router()

# Tracks the single "main" bot message per user (telegram_id -> message_id)
_main_msgs: dict[int, int] = {}


def set_main_msg(telegram_id: int, msg_id: int):
    _main_msgs[telegram_id] = msg_id


def is_owner(telegram_id: int) -> bool:
    """Kept for import compatibility with auth.py."""
    return True


def _welcome_text(user) -> str:
    monitoring = "✅" if user.is_working else "❌"
    auth = "✅" if user.is_authorized else "❌"
    return (
        f"<b>Привет, {user.first_name or 'риелтор'}!</b>\n\n"
        f"Статус мониторинга: {monitoring}\n\n"
        f"Статус аккаунта: {auth}"
    )


async def _replace_main(message: Message, text: str, reply_markup=None):
    """Delete old main message and send a new one (reply keyboard or no markup)."""
    tid = message.chat.id
    user = await get_user(tid)
    is_working = user.is_working if user else False
    old_id = _main_msgs.pop(tid, None)
    msg = await message.bot.send_message(
        tid, text, parse_mode="HTML", reply_markup=reply_markup or bottom_menu(is_working)
    )
    _main_msgs[tid] = msg.message_id
    if old_id:
        try:
            await message.bot.delete_message(tid, old_id)
        except Exception:
            pass


async def _edit_main(message: Message, text: str, inline_markup=None):
    """Edit the stored main message with inline keyboard, or send new one."""
    tid = message.chat.id
    msg_id = _main_msgs.get(tid)
    if msg_id:
        try:
            await message.bot.edit_message_text(
                chat_id=tid, message_id=msg_id,
                text=text, parse_mode="HTML",
                reply_markup=inline_markup,
            )
            return
        except Exception:
            try:
                await message.bot.delete_message(tid, msg_id)
            except Exception:
                pass
            _main_msgs.pop(tid, None)
    msg = await message.answer(text, parse_mode="HTML", reply_markup=inline_markup)
    _main_msgs[tid] = msg.message_id


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
        return

    await _replace_main(message, _welcome_text(user))


# ── Inline "main_menu" callback (Назад from sub-menus) ───────────────

@router.callback_query(F.data == "main_menu")
async def cb_main_menu(call: CallbackQuery):
    user = await get_user(call.from_user.id)
    if not user:
        return
    await call.answer()
    await _replace_main(call.message, _welcome_text(user))


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

    await _replace_main(message, status_text)


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


