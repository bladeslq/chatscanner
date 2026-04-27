from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from config import PROPERTY_TYPES, TRANSACTION_TYPES


def main_menu(is_working: bool) -> InlineKeyboardMarkup:
    work_text = "🔴 Остановить мониторинг" if is_working else "🟢 Я на работе (начать мониторинг)"
    kb = InlineKeyboardBuilder()
    kb.button(text=work_text, callback_data="toggle_work")
    kb.button(text="👥 Клиенты", callback_data="clients_menu")
    kb.button(text="📡 Мониторинг чатов", callback_data="chats_menu")
    kb.adjust(1)
    return kb.as_markup()


def clients_menu(clients: list) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for c in clients:
        status = "✅" if c.is_active else "❌"
        kb.button(text=f"{status} {c.name}", callback_data=f"client_view:{c.id}")
    kb.button(text="➕ Добавить клиента", callback_data="client_add")
    kb.button(text="◀️ Назад", callback_data="main_menu")
    kb.adjust(1)
    return kb.as_markup()


def client_actions(client_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Редактировать", callback_data=f"client_edit:{client_id}")
    kb.button(text="🏠 Подходящие объекты", callback_data=f"client_matches:{client_id}:0")
    kb.button(text="🗑 Удалить", callback_data=f"client_delete:{client_id}")
    kb.button(text="◀️ К списку", callback_data="clients_menu")
    kb.adjust(2, 1, 1)
    return kb.as_markup()


def transaction_type_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for k, v in TRANSACTION_TYPES.items():
        kb.button(text=v, callback_data=f"tr_type:{k}")
    kb.button(text="Любой", callback_data="tr_type:any")
    kb.adjust(2)
    return kb.as_markup()


def property_type_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for k, v in PROPERTY_TYPES.items():
        kb.button(text=v, callback_data=f"prop_type:{k}")
    kb.button(text="Любой", callback_data="prop_type:any")
    kb.adjust(2)
    return kb.as_markup()


def districts_kb(selected: list) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    from config import KAZAN_DISTRICTS
    for d in KAZAN_DISTRICTS:
        mark = "✅ " if d in selected else ""
        kb.button(text=f"{mark}{d}", callback_data=f"district:{d}")
    kb.button(text="Все районы", callback_data="district:all")
    kb.button(text="✅ Готово", callback_data="district:done")
    kb.adjust(2)
    return kb.as_markup()


def skip_kb(callback: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Пропустить", callback_data=callback)
    return kb.as_markup()


def chats_menu(monitored_chats: list) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if monitored_chats:
        for c in monitored_chats:
            kb.button(text=f"❌ {c.chat_name}", callback_data=f"chat_remove:{c.chat_id}")
    kb.button(text="➕ Добавить чат из списка", callback_data="chats_add_list")
    kb.button(text="◀️ Назад", callback_data="main_menu")
    kb.adjust(1)
    return kb.as_markup()


def paginated_chats_kb(dialogs: list, page: int, selected_ids: list) -> InlineKeyboardMarkup:
    PAGE_SIZE = 8
    start = page * PAGE_SIZE
    end = start + PAGE_SIZE
    page_dialogs = dialogs[start:end]

    kb = InlineKeyboardBuilder()
    for d in page_dialogs:
        mark = "✅ " if d["id"] in selected_ids else ""
        name = d["name"][:35]
        kb.button(text=f"{mark}{name}", callback_data=f"chat_toggle:{d['id']}")
    kb.adjust(1)

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"chats_page:{page-1}"))
    nav.append(InlineKeyboardButton(text=f"{page+1}/{(len(dialogs)-1)//PAGE_SIZE+1}", callback_data="noop"))
    if end < len(dialogs):
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"chats_page:{page+1}"))
    if nav:
        kb.row(*nav)

    kb.row(InlineKeyboardButton(text="✅ Сохранить выбор", callback_data="chats_save"))
    return kb.as_markup()


def confirm_kb(yes_cb: str, no_cb: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Да", callback_data=yes_cb)
    kb.button(text="❌ Нет", callback_data=no_cb)
    kb.adjust(2)
    return kb.as_markup()


def back_kb(callback: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data=callback)
    return kb.as_markup()
