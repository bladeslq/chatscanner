"""Client management: add, view, edit, delete clients with their requirements."""
from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from database.db import get_clients, get_client, create_client, update_client, delete_client, get_user, get_client_matches
from bot.keyboards.menus import (
    clients_menu, client_actions,
    districts_kb, skip_kb, back_kb, confirm_kb,
)
from config import PROPERTY_TYPES, TRANSACTION_TYPES, KAZAN_DISTRICTS

router = Router()


class ClientEdit(StatesGroup):
    choosing_field = State()
    editing_value = State()
    editing_districts = State()
    editing_property = State()


class ClientAdd(StatesGroup):
    name = State()
    phone = State()
    rooms_min = State()
    rooms_max = State()
    price_min = State()
    price_max = State()
    districts = State()
    notes = State()


async def _next(bot: Bot, state: FSMContext, text: str, reply_markup=None):
    """Edit the single persistent menu message."""
    data = await state.get_data()
    await bot.edit_message_text(
        chat_id=data["menu_chat_id"],
        message_id=data["menu_msg_id"],
        text=text,
        reply_markup=reply_markup,
        parse_mode="HTML",
    )


async def _del(message: Message):
    try:
        await message.delete()
    except Exception:
        pass


# ── List ──────────────────────────────────────────────────────────────

@router.callback_query(F.data == "clients_menu")
async def cb_clients_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    user = await get_user(call.from_user.id)
    clients = await get_clients(user.id, active_only=True)
    text = (
        "<b>Здесь список ваших клиентов</b>\n\n"
        "Укажите новых клиентов или нажмите на текущие, чтобы редактировать их.\n\n"
        f"<b>Активных: {len(clients)}</b>"
    )
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=clients_menu(clients))


# ── View ──────────────────────────────────────────────────────────────

def _client_card(client, prefix: str = "") -> str:
    reqs = "\n".join(client.requirements_text().split(" | "))
    districts_str = ", ".join(client.districts) if client.districts else "Любой"
    header = f"{prefix}\n\n" if prefix else ""
    notes_str = f"\n{client.notes}" if client.notes else ""
    return (
        f"{header}"
        f"<b>{client.name}</b>\n"
        f"Номер для связи — {client.phone or 'не указан'}\n\n"
        f"<b>Требования:</b>\n"
        f"{reqs}\n"
        f"{districts_str}"
        f"{notes_str}"
    )


@router.callback_query(F.data.startswith("client_view:"))
async def cb_client_view(call: CallbackQuery, state: FSMContext):
    await state.clear()
    client_id = int(call.data.split(":")[1])
    client = await get_client(client_id)
    if not client:
        await call.answer("Клиент не найден")
        return
    await call.message.edit_text(_client_card(client), parse_mode="HTML", reply_markup=client_actions(client_id))


# ── Matches ───────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("client_matches:"))
async def cb_client_matches(call: CallbackQuery):
    parts = call.data.split(":")
    client_id = int(parts[1])
    page = int(parts[2]) if len(parts) > 2 else 0

    client = await get_client(client_id)
    matches = await get_client_matches(client_id)

    if not matches:
        await call.message.edit_text(
            f"<b>{client.name}</b>\n\n"
            "📭 Подходящих объектов пока нет\n\n"
            "Объекты накапливаются автоматически во время мониторинга",
            parse_mode="HTML",
            reply_markup=back_kb(f"client_view:{client_id}"),
        )
        return

    total = len(matches)
    page = max(0, min(page, total - 1))
    match = matches[page]
    text = _build_match_card(match, page, total)
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=_match_nav_kb(client_id, page, total))


def _build_match_card(match, page: int, total: int) -> str:
    d = match.extracted_data or {}
    prop = PROPERTY_TYPES.get(d.get("property_type", ""), d.get("property_type") or "—")
    tr = TRANSACTION_TYPES.get(d.get("transaction_type", ""), d.get("transaction_type") or "—")

    price = d.get("price")
    if price:
        price_str = f"{int(price):,}".replace(",", " ") + " ₽"
        if d.get("price_includes_utilities") is True:
            price_str += " (вкл.)"
        elif d.get("price_includes_utilities") is False:
            price_str += " + КУ"
    else:
        price_str = "—"

    rooms = d.get("rooms")
    rooms_str = f"{rooms}-комн." + (" (евро)" if d.get("euro_format") else "") if rooms else "—"
    area = d.get("area")
    area_str = f"{area} м²" if area else "—"

    deposit = d.get("deposit")
    dep_str = f"{int(deposit):,}".replace(",", " ") + " ₽" if deposit else None
    if dep_str and d.get("deposit_negotiable"):
        dep_str += " (делимый)"

    district = d.get("district") or "—"
    address = d.get("address") or ""
    complex_name = d.get("complex") or ""
    contact = d.get("contact") or "—"
    description = d.get("description") or ""
    score = match.match_score or 0
    stars = "⭐" * (score // 20)
    dt = match.sent_at.strftime("%d.%m %H:%M") if match.sent_at else ""

    lines = [
        f"🏠 <b>{tr} · {prop}</b>   <i>{page + 1}/{total}</i>",
        f"📌 {match.chat_name}  ·  {dt}  ·  {score}% {stars}",
        "",
        f"🚪 {rooms_str}  ·  📐 {area_str}",
        f"💰 {price_str}",
    ]
    if dep_str:
        lines.append(f"🔒 Залог: {dep_str}")
    lines += [f"🗺 {district}"]
    if address:
        lines.append(f"📍 {address}")
    if complex_name:
        lines.append(f"🏢 ЖК {complex_name}")
    tenant = d.get("tenant_requirements")
    if tenant:
        lines.append(f"👥 {tenant}")
    lines.append(f"📞 {contact}")
    if description:
        lines += ["", f"<i>{description[:250]}</i>"]
    if match.message_text:
        lines += ["", "— — — — — — — — — —", f"<code>{match.message_text[:400]}</code>"]
    return "\n".join(lines)


def _match_nav_kb(client_id: int, page: int, total: int):
    kb = InlineKeyboardBuilder()
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"client_matches:{client_id}:{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{total}", callback_data="noop"))
    if page < total - 1:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"client_matches:{client_id}:{page + 1}"))
    if nav:
        kb.row(*nav)
    kb.row(InlineKeyboardButton(text="◀️ К клиенту", callback_data=f"client_view:{client_id}"))
    return kb.as_markup()


# ── Delete ────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("client_delete:"))
async def cb_client_delete(call: CallbackQuery):
    client_id = int(call.data.split(":")[1])
    client = await get_client(client_id)
    await call.message.edit_text(
        f"🗑 Удалить клиента <b>{client.name}</b>?",
        parse_mode="HTML",
        reply_markup=confirm_kb(f"client_delete_yes:{client_id}", "clients_menu"),
    )


@router.callback_query(F.data.startswith("client_delete_yes:"))
async def cb_client_delete_yes(call: CallbackQuery):
    client_id = int(call.data.split(":")[1])
    await delete_client(client_id)
    await call.answer("✅ Клиент удалён")
    user = await get_user(call.from_user.id)
    clients = await get_clients(user.id, active_only=True)
    await call.message.edit_text(
        "<b>Здесь список ваших клиентов</b>\n\n"
        "Укажите новых клиентов или нажмите на текущие, чтобы редактировать их.\n\n"
        f"<b>Активных: {len(clients)}</b>",
        parse_mode="HTML",
        reply_markup=clients_menu(clients),
    )


# ── Edit ──────────────────────────────────────────────────────────────

EDIT_FIELDS = {
    "name": "Имя",
    "phone": "Телефон",
    "min_rooms": "Мин. комнат",
    "max_rooms": "Макс. комнат",
    "min_price": "Мин. цена (₽)",
    "max_price": "Макс. цена (₽)",
    "districts": "Районы",
    "notes": "Заметки",
}


def _edit_fields_kb(client_id: int):
    kb = InlineKeyboardBuilder()
    for field, label in EDIT_FIELDS.items():
        kb.button(text=label, callback_data=f"edit_field:{field}")
    kb.button(text="Назад", callback_data=f"client_view:{client_id}")
    kb.adjust(2)
    return kb.as_markup()


def _with_back(markup, callback: str = "edit_back_fields"):
    """Append a Назад row to any InlineKeyboardMarkup."""
    kb = InlineKeyboardBuilder.from_markup(markup)
    kb.row(InlineKeyboardButton(text="Назад", callback_data=callback))
    return kb.as_markup()


@router.callback_query(F.data.startswith("client_edit:"))
async def cb_client_edit(call: CallbackQuery, state: FSMContext):
    client_id = int(call.data.split(":")[1])
    client = await get_client(client_id)
    if not client:
        await call.answer("Клиент не найден")
        return
    await state.update_data(
        edit_client_id=client_id,
        menu_chat_id=call.message.chat.id,
        menu_msg_id=call.message.message_id,
    )
    await state.set_state(ClientEdit.choosing_field)
    await call.message.edit_text(
        f"<b>Редактирование: {client.name}</b>\n\nЧто изменить?",
        parse_mode="HTML",
        reply_markup=_edit_fields_kb(client_id),
    )


@router.callback_query(F.data == "edit_back_fields")
async def cb_edit_back_fields(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    client_id = data.get("edit_client_id")
    client = await get_client(client_id)
    await state.set_state(ClientEdit.choosing_field)
    await call.message.edit_text(
        f"<b>Редактирование: {client.name}</b>\n\nЧто изменить?",
        parse_mode="HTML",
        reply_markup=_edit_fields_kb(client_id),
    )
    await call.answer()


@router.callback_query(F.data.startswith("edit_field:"), ClientEdit.choosing_field)
async def cb_edit_field(call: CallbackQuery, state: FSMContext):
    field = call.data.split(":")[1]
    await state.update_data(edit_field=field)
    label = EDIT_FIELDS.get(field, field)

    if field == "districts":
        data = await state.get_data()
        client = await get_client(data["edit_client_id"])
        current = list(client.districts or [])
        await state.update_data(selected_districts=current)
        await state.set_state(ClientEdit.editing_districts)
        await call.message.edit_text(
            "Районы (выбери нужные, нажми Готово):",
            parse_mode="HTML",
            reply_markup=_with_back(districts_kb(current)),
        )
    else:
        await state.set_state(ClientEdit.editing_value)
        back_kb = InlineKeyboardBuilder()
        back_kb.button(text="Назад", callback_data="edit_back_fields")
        await call.message.edit_text(
            f"Введи новое значение для <b>{label}</b>:\n\n(или <code>-</code> чтобы очистить)",
            parse_mode="HTML",
            reply_markup=back_kb.as_markup(),
        )


@router.callback_query(F.data.startswith("district:"), ClientEdit.editing_districts)
async def cb_edit_district(call: CallbackQuery, state: FSMContext):
    val = call.data.split(":", 1)[1]
    data = await state.get_data()
    selected = list(data.get("selected_districts", []))

    if val == "all":
        selected = []
        await state.update_data(selected_districts=selected)
        await call.message.edit_reply_markup(reply_markup=_with_back(districts_kb(selected)))
    elif val == "done":
        client_id = data["edit_client_id"]
        await update_client(client_id, districts=selected if selected else None)
        client = await get_client(client_id)
        await state.clear()
        await call.message.edit_text(
            _client_card(client, "✅ Районы обновлены!"),
            parse_mode="HTML",
            reply_markup=client_actions(client_id),
        )
    else:
        if val in selected:
            selected.remove(val)
        else:
            selected.append(val)
        await state.update_data(selected_districts=selected)
        await call.message.edit_reply_markup(reply_markup=_with_back(districts_kb(selected)))



@router.callback_query(F.data.startswith("prop_type:"), ClientEdit.editing_property)
async def cb_edit_property(call: CallbackQuery, state: FSMContext):
    val = call.data.split(":")[1]
    data = await state.get_data()
    client_id = data["edit_client_id"]
    await update_client(client_id, property_type=None if val == "any" else val)
    client = await get_client(client_id)
    await state.clear()
    await call.message.edit_text(
        _client_card(client, "✅ Обновлено!"),
        parse_mode="HTML",
        reply_markup=client_actions(client_id),
    )


@router.message(ClientEdit.editing_value)
async def process_edit_value(message: Message, state: FSMContext):
    data = await state.get_data()
    client_id = data["edit_client_id"]
    field = data["edit_field"]
    raw = message.text.strip()
    value = None if raw == "-" else raw

    numeric_int = {"min_rooms", "max_rooms"}
    numeric_float = {"min_price", "max_price", "min_area", "max_area"}

    if value is not None:
        if field in numeric_int:
            try:
                value = int(value.replace(" ", ""))
            except ValueError:
                await _del(message)
                await _next(message.bot, state, "❌ Введи целое число:")
                return
        elif field in numeric_float:
            try:
                value = float(value.replace(" ", ""))
            except ValueError:
                await _del(message)
                await _next(message.bot, state, "❌ Введи число:")
                return

    data = await state.get_data()
    chat_id = data.get("menu_chat_id", message.chat.id)
    msg_id = data.get("menu_msg_id")
    await update_client(client_id, **{field: value})
    client = await get_client(client_id)
    await _del(message)
    await state.clear()
    text = _client_card(client, "✅ Обновлено!")
    await message.bot.edit_message_text(
        chat_id=chat_id,
        message_id=msg_id,
        text=text,
        reply_markup=client_actions(client_id),
        parse_mode="HTML",
    )


# ── Add flow ──────────────────────────────────────────────────────────

@router.callback_query(F.data == "client_add")
async def cb_client_add(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.update_data(
        menu_chat_id=call.message.chat.id,
        menu_msg_id=call.message.message_id,
    )
    await state.set_state(ClientAdd.name)
    await call.message.edit_text(
        "➕ <b>Новый клиент</b>\n\nИмя клиента (например: Иванов Иван):",
        parse_mode="HTML",
    )


@router.message(ClientAdd.name)
async def process_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await _del(message)
    await state.set_state(ClientAdd.phone)
    await _next(message.bot, state, "📞 Телефон клиента:", skip_kb("skip_phone"))


@router.callback_query(F.data == "skip_phone", ClientAdd.phone)
async def skip_phone(call: CallbackQuery, state: FSMContext):
    await state.update_data(phone=None)
    await state.set_state(ClientAdd.rooms_min)
    await call.message.edit_text("🚪 Минимальное кол-во комнат:", reply_markup=skip_kb("skip_rooms_min"))


@router.message(ClientAdd.phone)
async def process_phone(message: Message, state: FSMContext):
    await state.update_data(phone=message.text.strip())
    await _del(message)
    await state.set_state(ClientAdd.rooms_min)
    await _next(message.bot, state, "🚪 Минимальное кол-во комнат:", skip_kb("skip_rooms_min"))


@router.callback_query(F.data == "skip_rooms_min", ClientAdd.rooms_min)
async def skip_rooms_min(call: CallbackQuery, state: FSMContext):
    await state.update_data(min_rooms=None)
    await state.set_state(ClientAdd.rooms_max)
    await call.message.edit_text("🚪 Максимальное кол-во комнат:", reply_markup=skip_kb("skip_rooms_max"))


@router.message(ClientAdd.rooms_min)
async def process_rooms_min(message: Message, state: FSMContext):
    try:
        val = int(message.text.strip())
    except ValueError:
        await _del(message)
        await _next(message.bot, state, "❌ Введи только цифру:", skip_kb("skip_rooms_min"))
        return
    await state.update_data(min_rooms=val)
    await _del(message)
    await state.set_state(ClientAdd.rooms_max)
    await _next(message.bot, state, "🚪 Максимальное кол-во комнат:", skip_kb("skip_rooms_max"))


@router.callback_query(F.data == "skip_rooms_max", ClientAdd.rooms_max)
async def skip_rooms_max(call: CallbackQuery, state: FSMContext):
    await state.update_data(max_rooms=None)
    await state.set_state(ClientAdd.price_min)
    await call.message.edit_text(
        "💰 Минимальная цена (в рублях, напр. 3500000):",
        reply_markup=skip_kb("skip_price_min"),
    )


@router.message(ClientAdd.rooms_max)
async def process_rooms_max(message: Message, state: FSMContext):
    try:
        val = int(message.text.strip())
    except ValueError:
        await _del(message)
        await _next(message.bot, state, "❌ Введи только цифру:", skip_kb("skip_rooms_max"))
        return
    await state.update_data(max_rooms=val)
    await _del(message)
    await state.set_state(ClientAdd.price_min)
    await _next(message.bot, state, "💰 Минимальная цена (₽):", skip_kb("skip_price_min"))


@router.callback_query(F.data == "skip_price_min", ClientAdd.price_min)
async def skip_price_min(call: CallbackQuery, state: FSMContext):
    await state.update_data(min_price=None)
    await state.set_state(ClientAdd.price_max)
    await call.message.edit_text("💰 Максимальная цена (₽):", reply_markup=skip_kb("skip_price_max"))


@router.message(ClientAdd.price_min)
async def process_price_min(message: Message, state: FSMContext):
    try:
        val = float(message.text.strip().replace(" ", ""))
    except ValueError:
        await _del(message)
        await _next(message.bot, state, "❌ Введи только число:", skip_kb("skip_price_min"))
        return
    await state.update_data(min_price=val)
    await _del(message)
    await state.set_state(ClientAdd.price_max)
    await _next(message.bot, state, "💰 Максимальная цена (₽):", skip_kb("skip_price_max"))


@router.callback_query(F.data == "skip_price_max", ClientAdd.price_max)
async def skip_price_max(call: CallbackQuery, state: FSMContext):
    await state.update_data(max_price=None)
    await state.set_state(ClientAdd.districts)
    await state.update_data(selected_districts=[])
    await call.message.edit_text("🗺 Районы Казани (выбери один или несколько, или «Все»):", reply_markup=districts_kb([]))


@router.message(ClientAdd.price_max)
async def process_price_max(message: Message, state: FSMContext):
    try:
        val = float(message.text.strip().replace(" ", ""))
    except ValueError:
        await _del(message)
        await _next(message.bot, state, "❌ Введи только число:", skip_kb("skip_price_max"))
        return
    await state.update_data(max_price=val)
    await _del(message)
    await state.set_state(ClientAdd.districts)
    await state.update_data(selected_districts=[])
    await _next(message.bot, state, "🗺 Районы Казани (выбери один или несколько, или «Все»):", districts_kb([]))


@router.callback_query(F.data.startswith("district:"), ClientAdd.districts)
async def process_district(call: CallbackQuery, state: FSMContext):
    val = call.data.split(":", 1)[1]
    data = await state.get_data()
    selected = list(data.get("selected_districts", []))

    if val == "all":
        selected = []
        await state.update_data(selected_districts=selected)
        await call.message.edit_reply_markup(reply_markup=districts_kb(selected))
    elif val == "done":
        await state.update_data(districts=selected if selected else None)
        await state.set_state(ClientAdd.notes)
        await call.message.edit_text(
            "📝 Дополнительные пожелания (или пропусти):",
            reply_markup=skip_kb("skip_notes"),
        )
    else:
        if val in selected:
            selected.remove(val)
        else:
            selected.append(val)
        await state.update_data(selected_districts=selected)
        await call.message.edit_reply_markup(reply_markup=districts_kb(selected))


@router.callback_query(F.data == "skip_notes", ClientAdd.notes)
async def skip_notes(call: CallbackQuery, state: FSMContext):
    await state.update_data(notes=None)
    await _save_client(call.from_user.id, call.message.bot, state)


@router.message(ClientAdd.notes)
async def process_notes(message: Message, state: FSMContext):
    await state.update_data(notes=message.text.strip())
    await _del(message)
    await _save_client(message.from_user.id, message.bot, state)


async def _save_client(telegram_id: int, bot: Bot, state: FSMContext):
    data = await state.get_data()
    chat_id = data.get("menu_chat_id")
    msg_id = data.get("menu_msg_id")
    await state.clear()

    user = await get_user(telegram_id)
    client = await create_client(
        user_id=user.id,
        name=data.get("name"),
        phone=data.get("phone"),
        property_type="apartment",
        min_rooms=data.get("min_rooms"),
        max_rooms=data.get("max_rooms"),
        min_price=data.get("min_price"),
        max_price=data.get("max_price"),
        districts=data.get("selected_districts") or data.get("districts"),
        notes=data.get("notes"),
    )
    clients = await get_clients(user.id)
    text = _client_card(client, "✅ <b>Клиент добавлен!</b>")
    await bot.edit_message_text(
        chat_id=chat_id,
        message_id=msg_id,
        text=text,
        parse_mode="HTML",
        reply_markup=clients_menu(clients),
    )
