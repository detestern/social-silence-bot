"""
Связывает adapter.listen() с БД: сохраняет каждое пришедшее сообщение,
определяет tier и уведомляет.

  instant   — уведомление сразу, без ИИ (реплай/упоминание/личка)
  escalated — одиночный запрос к ИИ вне очереди, через _escalated_worker
  hourly    — сохраняется, разбор — в core/scheduler.py раз в N минут
  pending   — обрывок, ждущий склейки с соседними репликами (см. ниже)

Короткие сообщения (<=2 слова) копятся в буфере по (чат, отправитель) и
склеиваются в одно перед классификацией — иначе "Ребята" / "Важная инфа" /
"завтра совещание" анализировались бы по отдельности и теряли смысл.
"""
import asyncio
import dataclasses
import logging
from datetime import datetime

from aiogram import Bot
from sqlalchemy import select

from adapters.telegram import TelegramAdapter
from core.classifier import ClassifierError, classify_single
from core.context import build_ai_context
from core.notify import notify_important
from core.prefilter import classify_tier
from core.timeutil import format_time
from db.models import Channel, Message, Source, User
from db.session import get_session

logger = logging.getLogger(__name__)

REFRESH_INTERVAL_SECONDS = 30
FRAGMENT_DEBOUNCE_SECONDS = 7
FRAGMENT_MAX_WORDS = 2
RECONNECT_BASE_DELAY = 5
RECONNECT_MAX_DELAY = 300
USER_WAIT_RETRY_SECONDS = 10


class _MonitoredChannelsCache:
    """external_id -> (channel_id, kind, title, group_label), обновляется
    в фоне каждые REFRESH_INTERVAL_SECONDS — без этого /chats и /tag
    требовали бы перезапуска, чтобы применить изменения."""

    def __init__(self) -> None:
        self._map: dict[str, tuple[int, str, str, str | None]] = {}

    def ids(self) -> set[str]:
        return set(self._map.keys())

    def lookup(self, external_id: str) -> tuple[int, str, str, str | None] | None:
        return self._map.get(external_id)

    async def refresh_once(self, source_code: str = "telegram") -> None:
        async with get_session() as session:
            source = (await session.execute(select(Source).where(Source.code == source_code))).scalar_one()
            rows = (await session.execute(
                select(Channel).where(Channel.source_id == source.id, Channel.is_monitored == True)  # noqa: E712
            )).scalars().all()
        self._map = {c.external_id: (c.id, c.kind, c.title or "", c.group_label) for c in rows}

    async def refresh_loop(self, source_code: str = "telegram") -> None:
        while True:
            try:
                await self.refresh_once(source_code)
            except Exception:
                logger.exception("Не удалось обновить список мониторимых чатов")
            await asyncio.sleep(REFRESH_INTERVAL_SECONDS)


@dataclasses.dataclass
class _TierInput:
    """Замена NormalizedMessage для classify_tier() после склейки обрывков,
    когда текст/флаги уже не совпадают ни с одним исходным сообщением."""
    text: str
    is_reply_to_user: bool
    is_direct_mention: bool
    has_media: bool


@dataclasses.dataclass
class _EscalatedJob:
    db_msg: Message
    channel_external_id: str
    channel_kind: str
    channel_title: str
    channel_group: str | None


@dataclasses.dataclass
class _FragmentEntry:
    db_msg: Message
    channel_external_id: str
    channel_kind: str
    channel_title: str
    channel_group: str | None
    is_reply_to_user: bool
    is_direct_mention: bool
    has_media: bool


class _FragmentCoalescer:
    """Буферизует короткие сообщения по (channel_id, sender_id) и сбрасывает
    через FRAGMENT_DEBOUNCE_SECONDS тишины — тогда вызывается flush_cb."""

    def __init__(self) -> None:
        self._buffers: dict[tuple[int, int], list[_FragmentEntry]] = {}
        self._timers: dict[tuple[int, int], asyncio.Task] = {}
        self.flush_cb = None

    def has_pending(self, key: tuple[int, int]) -> bool:
        return key in self._buffers

    def add(self, key: tuple[int, int], entry: _FragmentEntry) -> None:
        self._buffers.setdefault(key, []).append(entry)
        old_timer = self._timers.get(key)
        if old_timer:
            old_timer.cancel()
        self._timers[key] = asyncio.create_task(self._debounce(key))

    async def _debounce(self, key: tuple[int, int]) -> None:
        try:
            await asyncio.sleep(FRAGMENT_DEBOUNCE_SECONDS)
        except asyncio.CancelledError:
            return
        entries = self._buffers.pop(key, None)
        self._timers.pop(key, None)
        if entries and self.flush_cb:
            await self.flush_cb(entries)

    async def shutdown(self) -> None:
        """Не теряем недосброшенные буферы при остановке — принудительно
        сбрасываем всё, что осталось."""
        timers = list(self._timers.values())
        for t in timers:
            t.cancel()
        await asyncio.gather(*timers, return_exceptions=True)
        remaining = list(self._buffers.items())
        self._buffers.clear()
        self._timers.clear()
        if self.flush_cb:
            for _key, entries in remaining:
                await self.flush_cb(entries)


async def _escalated_worker(
    queue: "asyncio.Queue[_EscalatedJob]", bot: Bot, adapter: TelegramAdapter, bot_id: int, user: User
) -> None:
    """Разбирает escalated-очередь строго по одному — параллельные запросы
    к ИИ нежелательны, а неограниченная очередь гарантирует, что ничего
    не потеряется, сколько бы сообщений ни пришло подряд."""
    while True:
        job = await queue.get()
        try:
            await _handle_escalated(bot, adapter, bot_id, user, job)
        except Exception:
            logger.exception("Необработанная ошибка при разборе escalated-сообщения")
        finally:
            queue.task_done()


async def _handle_escalated(bot: Bot, adapter: TelegramAdapter, bot_id: int, user: User, job: _EscalatedJob) -> None:
    db_msg = job.db_msg

    media = None
    if db_msg.has_media:
        media = await adapter.download_media_bytes(job.channel_external_id, int(db_msg.external_id))

    async with get_session() as session:
        context_text = await build_ai_context(session, user.id, job.channel_group)

    try:
        important = await classify_single(
            context_text, db_msg.sender_name, job.channel_title, job.channel_group, db_msg.text or "", media=media
        )
    except ClassifierError:
        await notify_important(
            bot, adapter, user.tg_notify_chat_id, bot_id,
            "😳 Ключи Gemini закончились — не смогла проверить это сообщение. Напиши Никите (/api).\n\n"
            f"Время: {format_time(db_msg.sent_at)}\nЧат: {job.channel_title}\nОт: {db_msg.sender_name or '—'}",
            job.channel_external_id, job.channel_kind, db_msg.external_id,
        )
        return

    async with get_session() as session:
        fresh = await session.get(Message, db_msg.id)
        fresh.importance = important
        fresh.classified_at = datetime.utcnow()
        await session.commit()

    if important:
        await notify_important(
            bot, adapter, user.tg_notify_chat_id, bot_id,
            "🔔 Важно\n\n"
            f"Время: {format_time(db_msg.sent_at)}\nЧат: {job.channel_title}\nОт: {db_msg.sender_name or '—'}",
            job.channel_external_id, job.channel_kind, db_msg.external_id,
        )


async def _wait_for_telegram_auth(adapter: TelegramAdapter) -> None:
    logged_once = False
    await adapter.client.connect()
    while not await adapter.client.is_user_authorized():
        if not logged_once:
            logger.info("Жду авторизации Telegram-сессии: набери /login в settings-боте.")
            logged_once = True
        await asyncio.sleep(USER_WAIT_RETRY_SECONDS)


async def _wait_for_ready_user() -> User:
    """Ждёт пользователя с настроенным alert-ботом — не завершается, а
    именно ждёт, иначе main.py (FIRST_COMPLETED) погасит оба бота на старте."""
    logged_once = False
    while True:
        async with get_session() as session:
            user = (await session.execute(select(User))).scalars().first()
        if user is not None and user.tg_notify_chat_id is not None:
            return user
        if not logged_once:
            logger.info("Жду пользователя: /chats в settings-боте и /start в alert-боте.")
            logged_once = True
        await asyncio.sleep(USER_WAIT_RETRY_SECONDS)


async def _finalize_and_notify(
    db_msg: Message, text: str, is_reply_to_user: bool, is_direct_mention: bool, has_media: bool,
    channel_kind: str, channel_external_id: str, channel_title: str, channel_group: str | None,
    bot: Bot, adapter: TelegramAdapter, bot_id: int, user: User,
    escalated_queue: "asyncio.Queue[_EscalatedJob]",
) -> None:
    """Общая точка принятия решения по tier — для обычных сообщений сразу
    и для canonical-сообщения после склейки обрывков."""
    tier_input = _TierInput(
        text=text, is_reply_to_user=is_reply_to_user,
        is_direct_mention=is_direct_mention, has_media=has_media,
    )
    tier = classify_tier(tier_input, channel_kind)

    async with get_session() as session:
        fresh = await session.get(Message, db_msg.id)
        fresh.tier = tier
        await session.commit()

    logger.info("Финализировано (tier=%s) сообщение id=%s из чата %s", tier, db_msg.id, channel_external_id)

    if tier == "instant":
        await notify_important(
            bot, adapter, user.tg_notify_chat_id, bot_id,
            "⚡️ Важно\n\n"
            f"Время: {format_time(db_msg.sent_at)}\nЧат: {channel_title}\nОт: {db_msg.sender_name or '—'}",
            channel_external_id, channel_kind, db_msg.external_id,
        )
    elif tier == "escalated":
        await escalated_queue.put(_EscalatedJob(db_msg, channel_external_id, channel_kind, channel_title, channel_group))


async def _process_one_message(
    msg, cache: _MonitoredChannelsCache, bot: Bot, adapter: TelegramAdapter, bot_id: int,
    user: User, escalated_queue: "asyncio.Queue[_EscalatedJob]", coalescer: _FragmentCoalescer,
) -> None:
    channel_info = cache.lookup(msg.channel_external_id)
    if channel_info is None:
        return  # чат сняли с мониторинга между событием и обработкой
    channel_id, channel_kind, channel_title, channel_group = channel_info

    async with get_session() as session:
        existing = (await session.execute(
            select(Message).where(Message.channel_id == channel_id, Message.external_id == msg.external_id)
        )).scalar_one_or_none()
        if existing:
            return  # дубликат события от Telethon

        sent_at = msg.sent_at.replace(tzinfo=None) if msg.sent_at.tzinfo else msg.sent_at
        db_msg = Message(
            channel_id=channel_id,
            external_id=msg.external_id,
            sender_name=msg.sender_name,
            text=msg.text,
            is_reply_to_user=msg.is_reply_to_user,
            is_direct_mention=msg.is_direct_mention,
            has_media=msg.has_media,
            sent_at=sent_at,
            tier="pending",
            raw_json=msg.raw,
        )
        session.add(db_msg)
        await session.commit()
        await session.refresh(db_msg)

    logger.info("Сохранено сообщение id=%s из чата %s", db_msg.id, msg.channel_external_id)

    word_count = len((msg.text or "").split())
    key = (channel_id, msg.sender_id) if msg.sender_id is not None else None

    if key is not None and (word_count <= FRAGMENT_MAX_WORDS or coalescer.has_pending(key)):
        coalescer.add(key, _FragmentEntry(
            db_msg=db_msg, channel_external_id=msg.channel_external_id, channel_kind=channel_kind,
            channel_title=channel_title, channel_group=channel_group,
            is_reply_to_user=msg.is_reply_to_user, is_direct_mention=msg.is_direct_mention,
            has_media=msg.has_media,
        ))
        return

    await _finalize_and_notify(
        db_msg, msg.text, msg.is_reply_to_user, msg.is_direct_mention, msg.has_media,
        channel_kind, msg.channel_external_id, channel_title, channel_group,
        bot, adapter, bot_id, user, escalated_queue,
    )


async def run_listener(adapter: TelegramAdapter, bot: Bot, bot_id: int) -> None:
    cache = _MonitoredChannelsCache()
    await cache.refresh_once()
    refresh_task = asyncio.create_task(cache.refresh_loop())

    user = await _wait_for_ready_user()
    await _wait_for_telegram_auth(adapter)

    escalated_queue: "asyncio.Queue[_EscalatedJob]" = asyncio.Queue()
    worker_task = asyncio.create_task(_escalated_worker(escalated_queue, bot, adapter, bot_id, user))

    coalescer = _FragmentCoalescer()

    async def _flush_fragments(entries: list[_FragmentEntry]) -> None:
        canonical = entries[-1]
        combined_text = "\n".join((e.db_msg.text or "") for e in entries)
        combined_reply = any(e.is_reply_to_user for e in entries)
        combined_mention = any(e.is_direct_mention for e in entries)
        combined_media = any(e.has_media for e in entries)

        async with get_session() as session:
            canon_db = await session.get(Message, canonical.db_msg.id)
            canon_db.text = combined_text
            for e in entries[:-1]:
                frag_db = await session.get(Message, e.db_msg.id)
                frag_db.merged_into_id = canon_db.id
            await session.commit()
            await session.refresh(canon_db)

        logger.info("Склеено %d обрывков в сообщение id=%s", len(entries), canon_db.id)

        await _finalize_and_notify(
            canon_db, combined_text, combined_reply, combined_mention, combined_media,
            canonical.channel_kind, canonical.channel_external_id, canonical.channel_title, canonical.channel_group,
            bot, adapter, bot_id, user, escalated_queue,
        )

    coalescer.flush_cb = _flush_fragments

    logger.info("Слушатель Telegram запущен, мониторится чатов: %d", len(cache.ids()))
    try:
        await bot.send_message(user.tg_notify_chat_id, f"🌸 Привет! Я на связи и присматриваю за {len(cache.ids())} чат(ами).")
    except Exception:
        logger.exception("Не удалось отправить приветственное сообщение о запуске")

    # Обрыв связи не должен убивать эту задачу целиком (main.py гасит всё
    # при завершении любой фоновой задачи) — переподключаемся с растущей
    # паузой; Ctrl+C (CancelledError) пробрасываем без изменений.
    delay = RECONNECT_BASE_DELAY
    had_failure = False
    try:
        while True:
            try:
                async for msg in adapter.listen(cache.ids):
                    if had_failure:
                        had_failure = False
                        delay = RECONNECT_BASE_DELAY
                        await bot.send_message(user.tg_notify_chat_id, "✅ Уже снова на связи, всё продолжается как обычно 💛")
                    await _process_one_message(msg, cache, bot, adapter, bot_id, user, escalated_queue, coalescer)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Связь с Telegram оборвалась, переподключаюсь через %d с.", delay)
                had_failure = True
                try:
                    await bot.send_message(
                        user.tg_notify_chat_id,
                        f"📶 Связь с Telegram на секунду прервалась, пробую снова (через {delay} с.)…",
                    )
                except Exception:
                    pass
                await asyncio.sleep(delay)
                delay = min(delay * 2, RECONNECT_MAX_DELAY)
    finally:
        refresh_task.cancel()
        worker_task.cancel()
        await coalescer.shutdown()
        await asyncio.gather(refresh_task, worker_task, return_exceptions=True)
