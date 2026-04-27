"""Monitored chats management: add chats from the list of user's groups."""
from aiogram import Router, F
from aiogram.types import CallbackQuery, Message
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from database.db import get_user, get_monitored_chats, add_monitored_chat, remove_monitored_chat
from bot.keyboards.menus import chats_menu, paginated_chats_kb, back_kb
from userbot.scanner import scanner
from bot.handlers.work_mode import _start_monitoring

router = Router()


class ChatsAdd(StatesGroup):
    browsing = State()


async def _del(message: Message):
    try:
        await message.delete()
    except Exception:
        pass


def _display_dialogs(data: dict) -> list:
    """Return filtered dialogs if search is active, else all."""
    filtered = data.get("filtered_dialogs")
    return filtered if filtered is not None else data.get("dialogs", [])


# ── Monitored chats list ───────────────────────────────────────────────

@router.callback_query(F.data == "chats_menu")
async def cb_chats_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    user = await get_user(call.from_user.id)
    monitored = await get_monitored_chats(user.id)
    count = len(monitored)
    text = (
        f"📡 <b>Мониторинг чатов</b>\n\n"
        f"Активных чатов: {count}\n\n"
        f"Нажми ❌ на чате, чтобы убрать.\n"
        f"«Добавить» — выбрать из твоих групп."
    )
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=chats_menu(monitored))


@router.callback_query(F.data.startswith("chat_remove:"))
async def cb_chat_remove(call: CallbackQuery):
    chat_id = int(call.data.split(":")[1])
    user = await get_user(call.from_user.id)
    await remove_monitored_chat(user.id, chat_id)
    await call.answer("Чат убран из мониторинга")
    if user.is_working:
        await _start_monitoring(call.from_user.id)
    monitored = await get_monitored_chats(user.id)
    await call.message.edit_reply_markup(reply_markup=chats_menu(monitored))


# ── Add chats with search ──────────────────────────────────────────────

@router.callback_query(F.data == "chats_add_list")
async def cb_chats_add_list(call: CallbackQuery, state: FSMContext):
    user = await get_user(call.from_user.id)
    if not user.is_authorized:
        await call.answer("⚠️ Сначала подключи аккаунт через /auth", show_alert=True)
        return

    await call.message.edit_text("⏳ Загружаю список твоих групп...")

    tg_client = scanner._clients.get(call.from_user.id)
    if not tg_client or not tg_client.is_connected():
        await call.message.edit_text(
            "❌ Юзербот не подключён. Перезапусти бот или выполни /auth",
            reply_markup=back_kb("chats_menu"),
        )
        return

    dialogs = await scanner.get_dialogs(call.from_user.id)
    if not dialogs:
        await call.message.edit_text(
            "😔 Не найдено групп/каналов.",
            reply_markup=back_kb("chats_menu"),
        )
        return

    monitored = await get_monitored_chats(user.id)
    selected_ids = [c.chat_id for c in monitored]

    await state.set_state(ChatsAdd.browsing)
    await state.update_data(
        dialogs=dialogs,
        selected_ids=selected_ids,
        page=0,
        filtered_dialogs=None,
        search_query="",
        menu_msg_id=call.message.message_id,
        menu_chat_id=call.message.chat.id,
    )
    await call.message.edit_text(
        f"📋 <b>Твои группы и каналы</b> ({len(dialogs)})\n\n"
        f"✅ — уже в мониторинге, нажми для переключения\n"
        f"\nНапиши часть названия для поиска",
        parse_mode="HTML",
        reply_markup=paginated_chats_kb(dialogs, 0, selected_ids),
    )


@router.message(ChatsAdd.browsing)
async def cb_chats_search_query(message: Message, state: FSMContext):
    """Filter dialogs by typed search query."""
    query = message.text.strip().lower()
    await _del(message)

    data = await state.get_data()
    dialogs = data.get("dialogs", [])
    selected_ids = data.get("selected_ids", [])

    if query:
        filtered = [d for d in dialogs if query in d["name"].lower()]
        await state.update_data(filtered_dialogs=filtered, search_query=query, page=0)
        display = filtered
        header = (
            f"🔍 <b>Поиск: «{query}»</b> — {len(filtered)} из {len(dialogs)}\n\n"
            f"✅ — уже в мониторинге, нажми для переключения\n"
            f"\nНапиши другой запрос или нажми кнопку для очистки"
        )
    else:
        await state.update_data(filtered_dialogs=None, search_query="", page=0)
        display = dialogs
        header = (
            f"📋 <b>Все группы</b> ({len(dialogs)})\n\n"
            f"✅ — уже в мониторинге, нажми для переключения\n"
            f"\nНапиши часть названия для поиска"
        )

    await message.bot.edit_message_text(
        chat_id=data["menu_chat_id"],
        message_id=data["menu_msg_id"],
        text=header,
        parse_mode="HTML",
        reply_markup=paginated_chats_kb(display, 0, selected_ids),
    )


@router.callback_query(F.data.startswith("chats_page:"))
async def cb_chats_page(call: CallbackQuery, state: FSMContext):
    page = int(call.data.split(":")[1])
    data = await state.get_data()
    selected_ids = data.get("selected_ids", [])
    display = _display_dialogs(data)
    await state.update_data(page=page)
    await call.message.edit_reply_markup(reply_markup=paginated_chats_kb(display, page, selected_ids))


@router.callback_query(F.data.startswith("chat_toggle:"))
async def cb_chat_toggle(call: CallbackQuery, state: FSMContext):
    chat_id = int(call.data.split(":")[1])
    data = await state.get_data()
    selected_ids = list(data.get("selected_ids", []))
    page = data.get("page", 0)
    display = _display_dialogs(data)

    if chat_id in selected_ids:
        selected_ids.remove(chat_id)
    else:
        selected_ids.append(chat_id)

    await state.update_data(selected_ids=selected_ids)
    await call.message.edit_reply_markup(reply_markup=paginated_chats_kb(display, page, selected_ids))


@router.callback_query(F.data == "chats_save")
async def cb_chats_save(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    dialogs = data.get("dialogs", [])
    selected_ids = data.get("selected_ids", [])
    await state.clear()

    user = await get_user(call.from_user.id)
    dialog_map = {d["id"]: d for d in dialogs}

    current_monitored = await get_monitored_chats(user.id)
    current_ids = {c.chat_id for c in current_monitored}

    to_add = set(selected_ids) - current_ids
    to_remove = current_ids - set(selected_ids)

    for chat_id in to_add:
        d = dialog_map.get(chat_id)
        if d:
            await add_monitored_chat(user.id, d["id"], d["name"], d.get("username"))

    for chat_id in to_remove:
        await remove_monitored_chat(user.id, chat_id)

    monitored = await get_monitored_chats(user.id)
    user = await get_user(call.from_user.id)
    if user.is_working:
        await _start_monitoring(call.from_user.id)

    await call.answer(f"✅ Сохранено: {len(monitored)} чатов")
    await call.message.edit_text(
        f"📡 <b>Мониторинг чатов</b>\n\nАктивных: {len(monitored)}",
        parse_mode="HTML",
        reply_markup=chats_menu(monitored),
    )


@router.callback_query(F.data == "noop")
async def cb_noop(call: CallbackQuery):
    await call.answer()
