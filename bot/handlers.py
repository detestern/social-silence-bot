"""
Команды settings-бота: /chats, /find, /school, /profile, /add_rule, /rules
и остальное — см. описания в /start.
"""
from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from telethon.errors import (
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)

from adapters.base import SourceAdapter
from adapters.telegram import _PRIVATE_LINK_RE, _PUBLIC_LINK_RE
from bot.keyboards import PAGE_SIZE, channels_keyboard
from core.rules_parser import parse_rule_text
from core.scheduler import debug_full_analysis
from core.digest import debug_daily_digest_now
from core.classifier import add_extra_keys
from core.schools import recompute_all_schools, recompute_school_profile
from core.timeutil import today_start_utc_naive
from db.models import Channel, Message, PriorityRule, ProfileSection, School, Source, User
from db.session import SOURCE_CODES, get_session

router = Router()

# source_code -> adapter instance, наполняется в main.py при старте
ADAPTERS: dict[str, SourceAdapter] = {}

PLATFORM_LABELS = {
    "telegram": "Telegram",
    "pachca": "Пачка",
    "gmail": "Gmail",
    "yandex_mail": "Яндекс.Почта",
}

# Выбранная платформа для /chats, /find, /school и т.д. — по её settings-чату.
# Дефолт telegram; переключение через /platform.
_CURRENT_PLATFORM: dict[int, str] = {}


def _current_platform(chat_id: int) -> str:
    return _CURRENT_PLATFORM.get(chat_id, "telegram")


# Черновики правил, ожидающие подтверждения — по её tg chat_id. Не переживает
# перезапуск, это ок — просто состояние диалога.
_PENDING_RULES: dict[int, dict] = {}

# Режим/фильтр, открытый у неё на экране — по chat_id. Не в callback_data:
# у Telegram лимит 64 байта, длинный тег в base64 туда не влезал.
_ACTIVE_VIEW: dict[int, tuple[str, str | None]] = {}

_EXPIRY_CYCLE = [(None, "Навсегда"), (7, "7 дн."), (14, "14 дн."), (30, "30 дн."), (90, "90 дн.")]

# Ссылка на alert-бота — выставляется из main.py. Нужна для /fresh: там
# уведомления шлются через alert-бота, а не через settings-бот.
ALERT_BOT = None
ALERT_BOT_ID: int | None = None


def set_alert_bot(bot, bot_id: int) -> None:
    global ALERT_BOT, ALERT_BOT_ID
    ALERT_BOT = bot
    ALERT_BOT_ID = bot_id


async def _get_or_create_user(session, tg_chat_id: int) -> User:
    """tg_chat_id — из SETTINGS-бота, не используется как адрес уведомлений:
    tg_notify_chat_id ставится отдельно, через /start в alert-боте."""
    user = (await session.execute(select(User))).scalars().first()
    if user is None:
        user = User(display_name="Тестовый пользователь", tg_notify_chat_id=None)
        session.add(user)
        await session.commit()
        await session.refresh(user)
    return user


async def _all_channels(session, user: User, source_code: str = "telegram") -> list[Channel]:
    source = (await session.execute(select(Source).where(Source.code == source_code))).scalar_one()
    return (await session.execute(
        select(Channel).where(Channel.user_id == user.id, Channel.source_id == source.id).order_by(Channel.id)
    )).scalars().all()


async def _sync_channels_from_platform(session, user: User, source_code: str) -> list[Channel]:
    adapter = ADAPTERS[source_code]
    discovered = await adapter.discover_channels()

    existing = await _all_channels(session, user, source_code)
    existing_by_ext_id = {c.external_id: c for c in existing}
    source = (await session.execute(select(Source).where(Source.code == source_code))).scalar_one()

    for d in discovered:
        if d.external_id not in existing_by_ext_id:
            session.add(Channel(
                user_id=user.id, source_id=source.id,
                external_id=d.external_id, title=d.title, kind=d.kind,
                is_monitored=False,
            ))
    await session.commit()
    return await _all_channels(session, user, source_code)


def _apply_mode(channels: list[Channel], mode: str, query: str | None) -> list[Channel]:
    if mode == "f" and query:
        q = query.lower()
        return [c for c in channels if c.title and q in c.title.lower()]
    if mode in ("m", "g"):
        return [c for c in channels if c.is_monitored]
    return channels


def _finish_label(mode: str) -> str:
    return "⬅️ Назад" if mode in ("m", "g") else "✔️ Готово"


@router.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "Привет! 🌸 Это бот, который поможет разгрести все рабочие чаты.\n\n"
        "С чего начать:\n"
        "1. /login — авторизовать чтение твоих чатов (если ещё не сделано)\n"
        "2. /chats — выбрать, какие чаты вообще присматривать\n"
        "3. Написать /start отдельному боту-уведомителю — туда будут прилетать самые важные вещи\n"
        "4. /profile — пара слов о себе (кто ты, что ведёшь), чтобы бот понимал, что тебя касается\n"
        "5. /school и /add_rule — по желанию, для особых случаев вроде временной ответственности за что-то\n\n"
        "Весь список команд — через «/» рядом с полем ввода. Если что-то непонятно — просто спроси у Никиты 💛"
    )


class LoginStates(StatesGroup):
    waiting_phone = State()
    waiting_code = State()
    waiting_password = State()


class SchoolStates(StatesGroup):
    waiting_name = State()


@router.message(Command("login"))
async def cmd_login(message: Message, state: FSMContext):
    adapter = ADAPTERS["telegram"]
    await adapter.client.connect()
    if await adapter.client.is_user_authorized():
        me = await adapter.client.get_me()
        phone = f"+{me.phone}" if getattr(me, "phone", None) else "неизвестный номер"
        await message.answer(
            f"Сейчас бот авторизован как {phone} — и читает чаты именно этого аккаунта.\n\n"
            "Это одна сессия на весь бот: войти под другим номером можно, но это ОТВЯЖЕТ "
            "текущий аккаунт — бот перестанет видеть его чаты и станет видеть чаты нового. "
            "Продолжить?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Да, войти под другим", callback_data="login_relogin"),
                InlineKeyboardButton(text="Отмена", callback_data="login_relogin_cancel"),
            ]]),
        )
        return

    await state.set_state(LoginStates.waiting_phone)
    await message.answer(
        "Пришли свой номер телефона в международном формате, например +79991234567.\n\n"
        "Дальше Telegram пришлёт тебе код — я попрошу его следующим шагом. "
        "Если передумаешь — /cancel."
    )


@router.callback_query(F.data == "login_relogin")
async def cb_login_relogin(callback: CallbackQuery, state: FSMContext):
    adapter = ADAPTERS["telegram"]
    try:
        await adapter.client.log_out()
    except Exception as exc:
        await callback.message.edit_text(f"Не получилось выйти из текущего аккаунта: {exc}")
        await callback.answer()
        return

    adapter._me_id = None  # сброс кэша "чей это аккаунт" — иначе он остался бы от прошлого логина
    await adapter.client.connect()
    await state.set_state(LoginStates.waiting_phone)
    await callback.message.edit_text(
        "Вышла из прошлого аккаунта. Пришли номер телефона нового — в формате +79991234567."
    )
    await callback.answer()


@router.callback_query(F.data == "login_relogin_cancel")
async def cb_login_relogin_cancel(callback: CallbackQuery):
    await callback.message.edit_text("Хорошо, оставила как есть 🌸")
    await callback.answer()


@router.message(Command("cancel"), StateFilter("*"))
async def cmd_cancel(message: Message, state: FSMContext):
    if await state.get_state() is None:
        await message.answer("Сейчас нечего отменять 🌸")
        return
    await state.clear()
    await message.answer("Отменила, можно начать заново 🌸")


@router.message(StateFilter(LoginStates.waiting_phone))
async def login_got_phone(message: Message, state: FSMContext):
    phone = message.text.strip()
    adapter = ADAPTERS["telegram"]
    try:
        sent = await adapter.client.send_code_request(phone)
    except PhoneNumberInvalidError:
        await message.answer("Похоже, номер невалидный 🤔 Попробуй ещё раз, с кодом страны (+7...).")
        return
    except Exception as exc:
        await message.answer(f"Не получилось отправить код: {exc}. Попробуй /login заново.")
        await state.clear()
        return

    await state.update_data(phone=phone, phone_code_hash=sent.phone_code_hash)
    await state.set_state(LoginStates.waiting_code)
    await message.answer("Код отправлен в Telegram — пришли его сюда цифрами, как получишь.")


@router.message(StateFilter(LoginStates.waiting_code))
async def login_got_code(message: Message, state: FSMContext):
    code = message.text.strip()
    data = await state.get_data()
    adapter = ADAPTERS["telegram"]
    try:
        await adapter.client.sign_in(phone=data["phone"], code=code, phone_code_hash=data["phone_code_hash"])
    except SessionPasswordNeededError:
        await state.set_state(LoginStates.waiting_password)
        await message.answer("Есть двухфакторка — пришли, пожалуйста, пароль от неё.")
        return
    except (PhoneCodeInvalidError, PhoneCodeExpiredError):
        await message.answer("Код неверный или устарел 🤔 Набери /login и попробуй заново.")
        await state.clear()
        return
    except Exception as exc:
        await message.answer(f"Не получилось войти: {exc}. Набери /login и попробуй заново.")
        await state.clear()
        return

    await state.clear()
    await message.answer("Готово! 🌸 Авторизация прошла, теперь можно /chats.")


@router.message(StateFilter(LoginStates.waiting_password))
async def login_got_password(message: Message, state: FSMContext):
    password = message.text
    adapter = ADAPTERS["telegram"]
    try:
        await adapter.client.sign_in(password=password)
    except Exception as exc:
        await message.answer(f"Пароль не подошёл: {exc}. Набери /login и попробуй заново.")
        await state.clear()
        return

    await state.clear()
    await message.answer("Готово! 🌸 Авторизация прошла, теперь можно /chats.")


@router.message(Command("platform"))
async def cmd_platform(message: Message):
    current = _current_platform(message.chat.id)
    rows = []
    for code in SOURCE_CODES:
        label = PLATFORM_LABELS.get(code, code)
        mark = "✅ " if code == current else ""
        suffix = "" if code in ADAPTERS else " (скоро)"
        rows.append([InlineKeyboardButton(text=f"{mark}{label}{suffix}", callback_data=f"platform:{code}")])

    await message.answer(
        f"Сейчас /chats, /school, /add_rule и остальное применяются к: {PLATFORM_LABELS.get(current, current)} 🌸\n"
        "Выбери другую платформу:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("platform:"))
async def cb_platform(callback: CallbackQuery):
    code = callback.data.split(":", 1)[1]
    label = PLATFORM_LABELS.get(code, code)
    if code not in ADAPTERS:
        await callback.answer(f"{label} пока не подключена — скоро будет 🌸", show_alert=True)
        return
    _CURRENT_PLATFORM[callback.message.chat.id] = code
    await callback.message.edit_text(f"Готово — теперь команды применяются к {label} 🌸")
    await callback.answer()


@router.message(Command("chats"))
async def cmd_chats(message: Message):
    platform = _current_platform(message.chat.id)
    if platform not in ADAPTERS:
        await message.answer(f"{PLATFORM_LABELS.get(platform, platform)} пока не подключена — скоро будет 🌸")
        return

    async with get_session() as session:
        user = await _get_or_create_user(session, message.chat.id)
        await message.answer("Подтягиваю список чатов…")
        try:
            channels = await _sync_channels_from_platform(session, user, platform)
        except RuntimeError as exc:
            await message.answer(str(exc))
            return

    if not channels:
        await message.answer("Не нашла ни одного чата 🤔 Странно — проверь /login.")
        return

    _ACTIVE_VIEW[message.chat.id] = ("a", None)
    await message.answer(
        "Отметь ✅, какие чаты присматривать — остальные я вообще не буду читать 🌸\n\n"
        "Если чатов много — набери /find текст, чтобы отфильтровать по названию.",
        reply_markup=channels_keyboard(channels, page=0),
    )


@router.message(Command("find"))
async def cmd_find(message: Message, command: CommandObject):
    query = (command.args or "").strip()
    if not query:
        await message.answer("Напиши так: /find химия — покажу чаты, у которых это есть в названии.")
        return

    async with get_session() as session:
        user = await _get_or_create_user(session, message.chat.id)
        all_channels = await _all_channels(session, user, _current_platform(message.chat.id))

    if not all_channels:
        await message.answer("Список чатов ещё не подтянут — сначала один раз набери /chats.")
        return

    matched = _apply_mode(all_channels, "f", query)
    if not matched:
        await message.answer(f"Ничего не нашла по «{query}» 🤔 Попробуй другое слово или часть названия.")
        return

    _ACTIVE_VIEW[message.chat.id] = ("f", query)
    await message.answer(
        f"Нашла {len(matched)} чат(ов) по «{query}»:",
        reply_markup=channels_keyboard(matched, page=0),
    )


@router.message(Command("monitored"))
async def cmd_monitored(message: Message):
    async with get_session() as session:
        user = await _get_or_create_user(session, message.chat.id)
        all_channels = await _all_channels(session, user, _current_platform(message.chat.id))

    monitored = _apply_mode(all_channels, "m", None)
    if not monitored:
        await message.answer("Сейчас ни один чат не отслеживается 🌸 Набери /chats, чтобы выбрать.")
        return

    _ACTIVE_VIEW[message.chat.id] = ("m", None)
    await message.answer(
        "Вот что сейчас отслеживается. Тап — снять с мониторинга (чат исчезнет из списка):",
        reply_markup=channels_keyboard(monitored, page=0, finish_label=_finish_label("m")),
    )


@router.message(Command("school"))
async def cmd_school(message: Message, state: FSMContext):
    await state.set_state(SchoolStates.waiting_name)
    await message.answer("Как называется школа? Просто пришли название 🌸 (/cancel — если передумаешь)")


@router.message(StateFilter(SchoolStates.waiting_name))
async def school_got_name(message: Message, state: FSMContext):
    name = message.text.strip()
    await state.clear()

    async with get_session() as session:
        user = await _get_or_create_user(session, message.chat.id)
        existing = (await session.execute(
            select(School).where(School.user_id == user.id, School.name == name)
        )).scalar_one_or_none()
        if existing:
            await message.answer(
                f'Школа «{name}» уже есть — используй /schools, чтобы посмотреть, '
                "или выбери другое название."
            )
            return
        school = School(user_id=user.id, name=name, computed_profile=None)
        session.add(school)
        await session.commit()
        await session.refresh(school)

    rows = []
    for code in SOURCE_CODES:
        label = PLATFORM_LABELS.get(code, code)
        suffix = "" if code in ADAPTERS else " (скоро)"
        rows.append([InlineKeyboardButton(text=f"{label}{suffix}", callback_data=f"schoolplat:{school.id}:{code}")])

    await message.answer(
        f'Школа «{name}» создана. С какой платформы подтянуть чаты для неё?',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("schoolplat:"))
async def cb_school_platform(callback: CallbackQuery):
    _, school_id_str, code = callback.data.split(":")
    school_id = int(school_id_str)
    label = PLATFORM_LABELS.get(code, code)

    if code not in ADAPTERS:
        await callback.answer(f"{label} пока не подключена — скоро будет 🌸", show_alert=True)
        return

    async with get_session() as session:
        school = await session.get(School, school_id)
        user = await _get_or_create_user(session, callback.message.chat.id)
        all_channels = await _all_channels(session, user, code)

    monitored = _apply_mode(all_channels, "g", None)
    if not monitored:
        await callback.message.edit_text(
            f"В {label} пока нет ни одного отслеживаемого чата — сначала /chats, потом заново /school."
        )
        await callback.answer()
        return

    _ACTIVE_VIEW[callback.message.chat.id] = ("g", school.name)
    _SCHOOL_WIZARD[callback.message.chat.id] = school.id
    finish_label, finish_callback = _finish_meta(callback.message.chat.id, "g")

    await callback.message.edit_text(
        f'Отметь ✅, какие чаты относятся к «{school.name}» (чат может быть только в одной школе):',
    )
    await callback.message.answer(
        "Выбирай чаты:",
        reply_markup=channels_keyboard(
            monitored, page=0, finish_label=finish_label, finish_callback=finish_callback,
            is_checked=lambda c: c.group_label == school.name,
        ),
    )
    await callback.answer()


@router.message(Command("schools"))
async def cmd_schools(message: Message):
    async with get_session() as session:
        user = await _get_or_create_user(session, message.chat.id)
        schools = (await session.execute(select(School).where(School.user_id == user.id))).scalars().all()
        all_channels = await _all_channels(session, user, _current_platform(message.chat.id))

    if not schools:
        await message.answer("Пока нет ни одной школы 🌸 /school — чтобы создать.")
        return

    lines = []
    for s in schools:
        chats = [c.title or c.external_id for c in all_channels if c.is_monitored and c.group_label == s.name]
        status = "профиль готов ✅" if s.computed_profile else "профиль ещё не посчитан ⏳"
        block = f"🏫 {s.name} ({status})"
        if chats:
            block += "\n" + "\n".join(f"   • {t}" for t in chats)
        else:
            block += "\n   (нет привязанных чатов)"
        lines.append(block)

    await message.answer("\n\n".join(lines))


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    """Короткий дашборд за сегодня: сколько сообщений пришло и сколько из
    них признано важными, по каким чатам — просто чтобы видеть, что
    система жива и вообще что-то делает, не заглядывая в терминал."""
    boundary = today_start_utc_naive()
    async with get_session() as session:
        rows = (await session.execute(
            select(Message).where(Message.sent_at >= boundary, Message.merged_into_id.is_(None))
        )).scalars().all()
        channels = {c.id: (c.title or "?") for c in (await session.execute(select(Channel))).scalars().all()}

    if not rows:
        await message.answer("Сегодня сообщений ещё не было 🌸")
        return

    important_count = sum(1 for m in rows if m.importance)
    by_channel: dict[str, int] = {}
    for m in rows:
        title = channels.get(m.channel_id, "?")
        by_channel[title] = by_channel.get(title, 0) + 1

    lines = [f"📊 За сегодня: {len(rows)} сообщений, из них важных — {important_count}.", "", "По чатам:"]
    for title, count in sorted(by_channel.items(), key=lambda kv: -kv[1]):
        lines.append(f"• {title}: {count}")

    await message.answer("\n".join(lines))



# chat_id -> school_id, пока идёт выбор чатов для новой/редактируемой школы
_SCHOOL_WIZARD: dict[int, int] = {}


def _finish_meta(chat_id: int, mode: str) -> tuple[str, str]:
    if mode == "g":
        school_id = _SCHOOL_WIZARD.get(chat_id)
        if school_id is not None:
            return "✔️ Готово, посчитать профиль", f"school_finish:{school_id}"
    return _finish_label(mode), "done"


@router.callback_query(F.data.startswith("toggle:"))
async def cb_toggle(callback: CallbackQuery):
    _, channel_id, page = callback.data.split(":")
    channel_id, page = int(channel_id), int(page)
    mode, query = _ACTIVE_VIEW.get(callback.message.chat.id, ("a", None))

    async with get_session() as session:
        channel = await session.get(Channel, channel_id)

        if mode == "g":
            channel.group_label = None if channel.group_label == query else query
        else:
            channel.is_monitored = not channel.is_monitored
        await session.commit()

        user = await _get_or_create_user(session, callback.message.chat.id)
        channels = await _all_channels(session, user, _current_platform(callback.message.chat.id))

    filtered = _apply_mode(channels, mode, query)
    is_checked = (lambda c: c.group_label == query) if mode == "g" else None

    max_page = max((len(filtered) - 1) // PAGE_SIZE, 0) if filtered else 0
    page = min(page, max_page)

    if not filtered:
        await callback.message.edit_text("Тут пока пусто 🌸", reply_markup=None)
        await callback.answer("Обновлено")
        return

    finish_label, finish_callback = _finish_meta(callback.message.chat.id, mode)
    await callback.message.edit_reply_markup(
        reply_markup=channels_keyboard(
            filtered, page=page, finish_label=finish_label, finish_callback=finish_callback, is_checked=is_checked
        )
    )
    await callback.answer("Обновлено")


@router.callback_query(F.data.startswith("page:"))
async def cb_page(callback: CallbackQuery):
    page = int(callback.data.split(":")[1])
    mode, query = _ACTIVE_VIEW.get(callback.message.chat.id, ("a", None))

    async with get_session() as session:
        user = await _get_or_create_user(session, callback.message.chat.id)
        channels = await _all_channels(session, user, _current_platform(callback.message.chat.id))

    filtered = _apply_mode(channels, mode, query)
    is_checked = (lambda c: c.group_label == query) if mode == "g" else None

    finish_label, finish_callback = _finish_meta(callback.message.chat.id, mode)
    await callback.message.edit_reply_markup(
        reply_markup=channels_keyboard(
            filtered, page=page, finish_label=finish_label, finish_callback=finish_callback, is_checked=is_checked
        )
    )
    await callback.answer()


@router.callback_query(F.data == "done")
async def cb_done(callback: CallbackQuery):
    _ACTIVE_VIEW.pop(callback.message.chat.id, None)
    await callback.message.edit_text("Готово 🌸 /chats, /monitored или /school — чтобы посмотреть ещё раз.")
    await callback.answer()


@router.callback_query(F.data.startswith("school_finish:"))
async def cb_school_finish(callback: CallbackQuery):
    school_id = int(callback.data.split(":")[1])
    _ACTIVE_VIEW.pop(callback.message.chat.id, None)
    _SCHOOL_WIZARD.pop(callback.message.chat.id, None)

    await callback.message.edit_text("Считаю профиль для этой школы… 🌸")
    await callback.answer()

    ok = await recompute_school_profile(school_id)
    if ok:
        await callback.message.answer("✅ Готово, школа настроена и профиль посчитан. /schools — посмотреть все.")
    else:
        await callback.message.answer(
            "Чаты сохранила, а вот профиль пока не посчитала (например, /profile ещё пустой) — "
            "как только заполнишь /profile, я пересчитаю всё автоматически."
        )


@router.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery):
    await callback.answer()


_INTERVAL_PRESETS = [15, 30, 60, 90, 120]
_HOUR_PRESETS = [18, 19, 20, 21, 22, 23]


def _settings_keyboard(user: User) -> InlineKeyboardMarkup:
    daily_label = "включена ✅" if user.daily_digest_enabled else "выключена ⛔"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"⏱ Разбор новых: раз в {user.hourly_interval_minutes} мин", callback_data="set_interval")],
        [InlineKeyboardButton(text=f"🕘 Дневная сводка в {user.daily_digest_hour}:00 (МСК)", callback_data="set_hour")],
        [InlineKeyboardButton(text=f"🌙 Дневная сводка {daily_label}", callback_data="set_daily_toggle")],
        [InlineKeyboardButton(text="✔️ Готово", callback_data="settings_done")],
    ])


@router.message(Command("settings"))
async def cmd_settings(message: Message):
    async with get_session() as session:
        user = await _get_or_create_user(session, message.chat.id)
    await message.answer("Настройки расписания 🌸", reply_markup=_settings_keyboard(user))


@router.callback_query(F.data == "set_interval")
async def cb_set_interval(callback: CallbackQuery):
    async with get_session() as session:
        user = await _get_or_create_user(session, callback.message.chat.id)
        idx = _INTERVAL_PRESETS.index(user.hourly_interval_minutes) if user.hourly_interval_minutes in _INTERVAL_PRESETS else -1
        user.hourly_interval_minutes = _INTERVAL_PRESETS[(idx + 1) % len(_INTERVAL_PRESETS)]
        await session.commit()
        await session.refresh(user)
    await callback.message.edit_reply_markup(reply_markup=_settings_keyboard(user))
    await callback.answer()


@router.callback_query(F.data == "set_hour")
async def cb_set_hour(callback: CallbackQuery):
    async with get_session() as session:
        user = await _get_or_create_user(session, callback.message.chat.id)
        idx = _HOUR_PRESETS.index(user.daily_digest_hour) if user.daily_digest_hour in _HOUR_PRESETS else -1
        user.daily_digest_hour = _HOUR_PRESETS[(idx + 1) % len(_HOUR_PRESETS)]
        await session.commit()
        await session.refresh(user)
    await callback.message.edit_reply_markup(reply_markup=_settings_keyboard(user))
    await callback.answer()


@router.callback_query(F.data == "set_daily_toggle")
async def cb_set_daily_toggle(callback: CallbackQuery):
    async with get_session() as session:
        user = await _get_or_create_user(session, callback.message.chat.id)
        user.daily_digest_enabled = not user.daily_digest_enabled
        await session.commit()
        await session.refresh(user)
    await callback.message.edit_reply_markup(reply_markup=_settings_keyboard(user))
    await callback.answer()


@router.callback_query(F.data == "settings_done")
async def cb_settings_done(callback: CallbackQuery):
    await callback.message.edit_text("Готово 🌸 /settings — если захочешь поменять ещё раз.")
    await callback.answer()


@router.message(Command("profile"))
async def cmd_profile(message: Message, command: CommandObject):
    async with get_session() as session:
        user = await _get_or_create_user(session, message.chat.id)
        section = (await session.execute(
            select(ProfileSection).where(ProfileSection.user_id == user.id, ProfileSection.channel_group.is_(None))
        )).scalar_one_or_none()

        if not command.args:
            text = section.text.strip() if section and section.text else None
            if text:
                await message.answer(f"Вот что сейчас записано о тебе:\n\n{text}\n\nЧтобы поменять: /profile новый текст")
            else:
                await message.answer(
                    "Про тебя пока ничего не записано 🌸\n\n"
                    "Напиши так: /profile Учитель химии, ведёт 10 и 11 классы в нескольких школах."
                )
            return

        new_text = command.args.strip()
        if section is None:
            session.add(ProfileSection(user_id=user.id, channel_group=None, text=new_text))
        else:
            section.text = new_text
        await session.commit()

    await message.answer("Записала, спасибо! Секунду, обновляю профиль по школам… 🌸")
    await recompute_all_schools(user.id)
    await message.answer("✅ Готово — профиль по всем школам пересчитан.")


def _rule_confirmation(chat_id: int) -> tuple[str, InlineKeyboardMarkup]:
    draft = _PENDING_RULES[chat_id]
    expiry_label = draft["expiry_label"]
    group_label = draft["group"] or "Все школы"

    text = (
        f'Поняла: «{draft["text"]}»\n'
        f"Школа: {group_label}\n"
        f"Истекает: {expiry_label}\n\n"
        "Проверь и сохрани, или поменяй параметры кнопками ниже 🌸"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"⏳ Срок: {expiry_label}", callback_data="ruleexp")],
        [InlineKeyboardButton(text=f"🏫 Школа: {group_label}", callback_data="rulegrp")],
        [
            InlineKeyboardButton(text="💾 Сохранить", callback_data="rulesave"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="rulecancel"),
        ],
    ])
    return text, kb


@router.message(Command("add_rule"))
async def cmd_add_rule(message: Message, command: CommandObject):
    if not command.args:
        await message.answer("Напиши так: /add_rule у нас экскурсия, я отвечаю за 10Б")
        return

    await message.answer("Секунду, разбираю…")
    parsed = await parse_rule_text(command.args)

    async with get_session() as session:
        user = await _get_or_create_user(session, message.chat.id)
        schools = (await session.execute(select(School).where(School.user_id == user.id))).scalars().all()
    groups = sorted(s.name for s in schools)

    days = parsed.get("expires_in_days")
    expiry_label = next((label for d, label in _EXPIRY_CYCLE if d == days), f"{days} дн." if days else "Навсегда")

    _PENDING_RULES[message.chat.id] = {
        "text": parsed["text"],
        "expiry_days": days,
        "expiry_label": expiry_label,
        "group": None,
        "group_cycle": ["Все школы"] + groups,
    }

    text, kb = _rule_confirmation(message.chat.id)
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "ruleexp")
async def cb_rule_expiry(callback: CallbackQuery):
    draft = _PENDING_RULES.get(callback.message.chat.id)
    if draft is None:
        await callback.answer("Ой, черновик уже не активен — начни заново через /add_rule 🌸", show_alert=True)
        return
    labels = [label for _, label in _EXPIRY_CYCLE]
    idx = labels.index(draft["expiry_label"]) if draft["expiry_label"] in labels else 0
    next_days, next_label = _EXPIRY_CYCLE[(idx + 1) % len(_EXPIRY_CYCLE)]
    draft["expiry_days"] = next_days
    draft["expiry_label"] = next_label

    text, kb = _rule_confirmation(callback.message.chat.id)
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "rulegrp")
async def cb_rule_group(callback: CallbackQuery):
    draft = _PENDING_RULES.get(callback.message.chat.id)
    if draft is None:
        await callback.answer("Ой, черновик уже не активен — начни заново через /add_rule 🌸", show_alert=True)
        return
    cycle = draft["group_cycle"]
    current = draft["group"] or "Все школы"
    idx = cycle.index(current) if current in cycle else 0
    next_choice = cycle[(idx + 1) % len(cycle)]
    draft["group"] = None if next_choice == "Все школы" else next_choice

    text, kb = _rule_confirmation(callback.message.chat.id)
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "rulesave")
async def cb_rule_save(callback: CallbackQuery):
    draft = _PENDING_RULES.pop(callback.message.chat.id, None)
    if draft is None:
        await callback.answer("Ой, черновик уже не активен — начни заново через /add_rule 🌸", show_alert=True)
        return

    expires_at = None
    if draft["expiry_days"]:
        expires_at = datetime.utcnow() + timedelta(days=draft["expiry_days"])

    async with get_session() as session:
        user = await _get_or_create_user(session, callback.message.chat.id)
        source = (await session.execute(
            select(Source).where(Source.code == _current_platform(callback.message.chat.id))
        )).scalar_one()
        session.add(PriorityRule(
            user_id=user.id,
            source_id=source.id,
            channel_group=draft["group"],
            text=draft["text"],
            expires_at=expires_at,
            is_active=True,
        ))
        await session.commit()

    await callback.message.edit_text(f'✅ Сохранила: «{draft["text"]}»')
    await callback.answer()


@router.callback_query(F.data == "rulecancel")
async def cb_rule_cancel(callback: CallbackQuery):
    _PENDING_RULES.pop(callback.message.chat.id, None)
    await callback.message.edit_text("Хорошо, отменила 🌸")
    await callback.answer()


@router.message(Command("rules"))
async def cmd_rules(message: Message):
    async with get_session() as session:
        user = await _get_or_create_user(session, message.chat.id)
        schools = (await session.execute(
            select(School).where(School.user_id == user.id).order_by(School.name)
        )).scalars().all()
        rules = (await session.execute(
            select(PriorityRule).where(PriorityRule.user_id == user.id, PriorityRule.is_active == True)  # noqa: E712
        )).scalars().all()

    if not schools and not rules:
        await message.answer("Пока пусто 🌸 /school — чтобы создать школу, /add_rule — чтобы добавить правило.")
        return

    now = datetime.utcnow()

    def _rule_line(num: int, r: PriorityRule) -> str:
        if r.expires_at:
            days_left = max((r.expires_at.date() - now.date()).days, 0)
            when = f"⏳ до {r.expires_at.strftime('%d.%m')} (ещё {days_left} дн.)"
        else:
            when = "♾ бессрочно"
        return f"{num}. «{r.text}» — {when}"

    rules_by_school: dict[str | None, list[PriorityRule]] = {}
    for r in rules:
        rules_by_school.setdefault(r.channel_group, []).append(r)

    blocks = []
    rows = []
    counter = 0

    for school in schools:
        lines = [f"🏫 {school.name}"]
        if school.computed_profile:
            lines.append(f"📄 Базовое (из профиля):\n{school.computed_profile}")
        else:
            lines.append("📄 Базовое: профиль ещё не посчитан — обнови /profile или пересоздай школу")

        for r in rules_by_school.pop(school.name, []):
            counter += 1
            lines.append(_rule_line(counter, r))
            rows.append([InlineKeyboardButton(text=f"❌ Удалить №{counter}", callback_data=f"delrule:{r.id}")])

        blocks.append("\n\n".join(lines))

    global_rules = rules_by_school.pop(None, [])
    if global_rules:
        lines = ["🌐 Все школы"]
        for r in global_rules:
            counter += 1
            lines.append(_rule_line(counter, r))
            rows.append([InlineKeyboardButton(text=f"❌ Удалить №{counter}", callback_data=f"delrule:{r.id}")])
        blocks.append("\n\n".join(lines))

    await message.answer(
        "📋 Активные правила:\n\n" + "\n\n---\n\n".join(blocks),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows) if rows else None,
    )


@router.callback_query(F.data.startswith("delrule:"))
async def cb_delete_rule(callback: CallbackQuery):
    rule_id = int(callback.data.split(":")[1])
    async with get_session() as session:
        rule = await session.get(PriorityRule, rule_id)
        if rule:
            await session.delete(rule)
            await session.commit()
    await callback.message.edit_text("Удалила ✅ /rules — посмотреть, что осталось.")
    await callback.answer()


@router.message(Command("fresh"))
async def cmd_fresh(message: Message):
    """Тестовая команда: прогоняет через ИИ все hourly-сообщения прямо
    сейчас, не дожидаясь обычного цикла. Курсор не трогает."""
    if ALERT_BOT is None:
        await message.answer(
            "Бот-уведомитель ещё не запущен/не настроен — уведомления слать некуда. "
            "Проверь main.py и .env (TG_ALERT_BOT_TOKEN)."
        )
        return
    await message.answer("Секунду, разбираю всё накопленное 🌸")
    await debug_full_analysis(ALERT_BOT, ADAPTERS["telegram"], ALERT_BOT_ID)


@router.message(Command("daily"))
async def cmd_daily(message: Message):
    """Тестовая команда: составляет дневную сводку прямо сейчас, не
    дожидаясь вечера. Курсор не трогает."""
    if ALERT_BOT is None:
        await message.answer(
            "Бот-уведомитель ещё не запущен/не настроен — уведомления слать некуда. "
            "Проверь main.py и .env (TG_ALERT_BOT_TOKEN)."
        )
        return
    await message.answer("Собираю дневную сводку прямо сейчас 🌸")
    await debug_daily_digest_now(ALERT_BOT)


@router.message(Command("api"))
async def cmd_api(message: Message, command: CommandObject):
    """Резервные ключи Gemini на лету, без доступа к серверу."""
    if not command.args:
        await message.answer(
            "Напиши так: /api ключ1,ключ2,ключ3 — через запятую без пробелов. "
            "Ключи добавятся как резервные и сразу же будут использоваться 💛"
        )
        return
    keys = [k.strip() for k in command.args.split(",") if k.strip()]
    try:
        added = add_extra_keys(keys)
    except Exception as exc:
        await message.answer(f"Ой, не получилось добавить ключи: {exc}")
        return
    if added:
        await message.answer(f"Готово, добавила {added} новых ключей — уже работают 🌸")
    else:
        await message.answer("Хм, эти ключи уже были в списке, ничего нового не добавила.")


@router.message(F.text.regexp(r"t\.me/"))
async def msg_add_by_link(message: Message):
    link = message.text.strip()
    if not (_PRIVATE_LINK_RE.search(link) or _PUBLIC_LINK_RE.search(link)):
        await message.answer(
            "Похоже на ссылку, но не смогла её разобрать 🤔 Нужна ссылка вида "
            "t.me/c/1234567890/456 — её даёт «Скопировать ссылку» на сообщении в чате."
        )
        return

    adapter = ADAPTERS["telegram"]
    channel_info = await adapter.resolve_link(link)
    if channel_info is None:
        await message.answer(
            "Не нашла такой чат — либо я (твой аккаунт) не состою в нём, "
            "либо это обычная (не супер-) группа, для которой Telegram не даёт ссылку на сообщение. "
            "Попробуй /find вместо этого."
        )
        return

    async with get_session() as session:
        user = await _get_or_create_user(session, message.chat.id)
        source = (await session.execute(select(Source).where(Source.code == "telegram"))).scalar_one()

        existing = (await session.execute(
            select(Channel).where(Channel.source_id == source.id, Channel.external_id == channel_info.external_id)
        )).scalar_one_or_none()

        if existing:
            existing.is_monitored = True
            await session.commit()
        else:
            session.add(Channel(
                user_id=user.id, source_id=source.id,
                external_id=channel_info.external_id, title=channel_info.title,
                kind=channel_info.kind, is_monitored=True,
            ))
            await session.commit()

    await message.answer(f"✅ Добавила, теперь слежу: «{channel_info.title}»")
