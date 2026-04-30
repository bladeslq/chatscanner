from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from config import PROPERTY_TYPES


def bottom_menu(is_working: bool) -> ReplyKeyboardMarkup:
    work_text = "Остановить мониторинг" if is_working else "Начать мониторинг"
    kb = ReplyKeyboardBuilder()
    kb.row(KeyboardButton(text=work_text))
    kb.row(
        KeyboardButton(text="Мои клиенты"),
        KeyboardButton(text="Выбор чатов"),
    )
    return kb.as_markup(resize_keyboard=True, persistent=True)


def main_menu(is_working: bool) -> InlineKeyboardMarkup:
    work_text = "Остановить мониторинг" if is_working else "Начать мониторинг"
    kb = InlineKeyboardBuilder()
    kb.button(text=work_text, callback_data="toggle_work")
    kb.button(text="Клиенты", callback_data="clients_menu")
    kb.button(text="Мониторинг чатов", callback_data="chats_menu")
    kb.adjust(1)
    return kb.as_markup()


def clients_menu(clients: list, page: int = 0) -> InlineKeyboardMarkup:
    PAGE_SIZE = 8
    start = page * PAGE_SIZE
    end = start + PAGE_SIZE
    page_clients = clients[start:end]

    kb = InlineKeyboardBuilder()
    for c in page_clients:
        kb.button(text=c.name, callback_data=f"client_view:{c.id}")
    kb.adjust(1)

    total_pages = max(1, (len(clients) - 1) // PAGE_SIZE + 1)
    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="<", callback_data=f"clients_page:{page-1}"))
        nav.append(InlineKeyboardButton(text=f"{page+1}/{total_pages}", callback_data="noop"))
        if end < len(clients):
            nav.append(InlineKeyboardButton(text=">", callback_data=f"clients_page:{page+1}"))
        kb.row(*nav)

    kb.row(
        InlineKeyboardButton(text="Новый клиент", callback_data="client_add"),
        InlineKeyboardButton(text="Назад", callback_data="main_menu"),
    )
    return kb.as_markup()


def client_actions(client_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Редактировать", callback_data=f"client_edit:{client_id}")
    kb.button(text="Подходящие объекты", callback_data=f"client_matches:{client_id}:0")
    kb.button(text="К списку клиентов", callback_data="clients_menu")
    kb.button(text="Удалить", callback_data=f"client_delete:{client_id}")
    kb.adjust(2, 1, 1)
    return kb.as_markup()


def property_type_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for k, v in PROPERTY_TYPES.items():
        kb.button(text=v, callback_data=f"prop_type:{k}")
    kb.button(text="Любой", callback_data="prop_type:any")
    kb.adjust(2)
    return kb.as_markup()


def districts_kb(selected: list, page: int = 0) -> InlineKeyboardMarkup:
    """Paginated district picker. 8 districts per page (4 rows × 2 cols)."""
    from config import KAZAN_DISTRICTS
    PAGE_SIZE = 8

    total_pages = max(1, (len(KAZAN_DISTRICTS) - 1) // PAGE_SIZE + 1)
    page = max(0, min(page, total_pages - 1))
    start = page * PAGE_SIZE
    end = start + PAGE_SIZE
    page_districts = KAZAN_DISTRICTS[start:end]

    kb = InlineKeyboardBuilder()
    for d in page_districts:
        mark = "✅ " if d in selected else ""
        kb.button(text=f"{mark}{d}", callback_data=f"district:{d}")
    kb.adjust(2)

    # Pagination row
    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="<", callback_data=f"districts_page:{page-1}"))
        nav.append(InlineKeyboardButton(text=f"{page+1}/{total_pages}", callback_data="noop"))
        if end < len(KAZAN_DISTRICTS):
            nav.append(InlineKeyboardButton(text=">", callback_data=f"districts_page:{page+1}"))
        kb.row(*nav)

    # Bottom controls
    kb.row(
        InlineKeyboardButton(text="Все районы", callback_data="district:all"),
        InlineKeyboardButton(text="Готово", callback_data="district:done"),
    )
    return kb.as_markup()


def skip_kb(callback: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Пропустить", callback_data=callback)
    return kb.as_markup()


def chats_menu(monitored_chats: list, page: int = 0) -> InlineKeyboardMarkup:
    PAGE_SIZE = 8
    start = page * PAGE_SIZE
    end = start + PAGE_SIZE
    page_chats = monitored_chats[start:end]

    kb = InlineKeyboardBuilder()
    for c in page_chats:
        kb.button(text=f"{c.chat_name} [удалить]", callback_data=f"chat_remove:{c.chat_id}")
    kb.adjust(1)

    total_pages = max(1, (len(monitored_chats) - 1) // PAGE_SIZE + 1)
    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="<", callback_data=f"monitored_page:{page-1}"))
        nav.append(InlineKeyboardButton(text=f"{page+1}/{total_pages}", callback_data="noop"))
        if end < len(monitored_chats):
            nav.append(InlineKeyboardButton(text=">", callback_data=f"monitored_page:{page+1}"))
        kb.row(*nav)

    kb.row(
        InlineKeyboardButton(text="Новый чат", callback_data="chats_add_list"),
        InlineKeyboardButton(text="Назад", callback_data="main_menu"),
    )
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
        nav.append(InlineKeyboardButton(text="<", callback_data=f"chats_page:{page-1}"))
    nav.append(InlineKeyboardButton(text=f"{page+1}/{(len(dialogs)-1)//PAGE_SIZE+1}", callback_data="noop"))
    if end < len(dialogs):
        nav.append(InlineKeyboardButton(text=">", callback_data=f"chats_page:{page+1}"))
    if nav:
        kb.row(*nav)

    kb.row(
        InlineKeyboardButton(text="Сохранить выбор", callback_data="chats_save"),
        InlineKeyboardButton(text="Назад", callback_data="chats_menu"),
    )
    return kb.as_markup()


def confirm_kb(yes_cb: str, no_cb: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Да", callback_data=yes_cb)
    kb.button(text="Нет", callback_data=no_cb)
    kb.adjust(2)
    return kb.as_markup()


def back_kb(callback: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Назад", callback_data=callback)
    return kb.as_markup()
