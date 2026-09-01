"""
Разбирает накопленные tier='hourly' сообщения батчем через ИИ (курсор в
processing_cursors двигается после каждого прогона) и уведомляет по
важным — поштучно, не одним текстом, потому что у каждого своя ссылка
на чат или пересылка.
"""
import asyncio
import logging
from datetime import datetime

from aiogram import Bot
from sqlalchemy import select

from adapters.telegram import TelegramAdapter
from core.classifier import ClassifierError, ClassifyBatchItem, classify_batch
from core.context import build_ai_context
from core.notify import notify_important
from core.timeutil import format_time
from db.models import Channel, Message, ProcessingCursor, User
from db.session import get_session

logger = logging.getLogger(__name__)


async def _get_cursor(session, user_id: int, cursor_type: str) -> ProcessingCursor:
    cursor = (await session.execute(
        select(ProcessingCursor).where(
            ProcessingCursor.user_id == user_id, ProcessingCursor.cursor_type == cursor_type
        )
    )).scalar_one_or_none()
    if cursor is None:
        cursor = ProcessingCursor(user_id=user_id, cursor_type=cursor_type, last_message_id=0)
        session.add(cursor)
        await session.commit()
        await session.refresh(cursor)
    return cursor


async def _channel_meta(session) -> dict:
    """channel.id -> (title, group_label, kind, external_id)."""
    rows = (await session.execute(select(Channel))).scalars().all()
    return {c.id: (c.title or "?", c.group_label, c.kind, c.external_id) for c in rows}


async def _build_items(pending: list[Message], meta: dict, adapter: TelegramAdapter) -> list[ClassifyBatchItem]:
    """Докачивает вложение только у сообщений с has_media, и только тут —
    в момент реального похода в ИИ, а не при получении события."""
    items = []
    for m in pending:
        title, group, _kind, external_id = meta.get(m.channel_id, ("?", None, "group", ""))
        media = None
        if m.has_media:
            media = await adapter.download_media_bytes(external_id, int(m.external_id))
        items.append(ClassifyBatchItem(
            id=m.id, channel_title=title, channel_group=group,
            sender_name=m.sender_name, text=m.text or "", media=media,
        ))
    return items


async def _notify_batch_results(
    bot: Bot, adapter: TelegramAdapter, bot_id: int, user: User,
    pending: list[Message], meta: dict, important_ids: set[int], label: str,
) -> None:
    important = [m for m in pending if m.id in important_ids]
    if not important:
        logger.info("%s: важных не найдено среди %d сообщений.", label, len(pending))
        return

    await bot.send_message(user.tg_notify_chat_id, f"{label} — {len(important)} важных из {len(pending)}:")
    for m in important:
        title, _group, kind, external_id = meta.get(m.channel_id, ("?", None, "group", ""))
        preview = (m.text or "")[:300]
        await notify_important(
            bot, adapter, user.tg_notify_chat_id, bot_id,
            f"Время: {format_time(m.sent_at)}\nЧат: {title}\nОт: {m.sender_name or '—'}\n{preview}",
            external_id, kind, m.external_id,
        )


async def _run_hourly_batch_once(bot: Bot, adapter: TelegramAdapter, bot_id: int) -> None:
    async with get_session() as session:
        user = (await session.execute(select(User))).scalars().first()
    if user is None:
        logger.info("Часовой батч: пользователя ещё нет, пропускаю.")
        return
    if user.tg_notify_chat_id is None:
        logger.info("Часовой батч: alert-бот ещё не подключён (/start там), пропускаю.")
        return

    async with get_session() as session:
        cursor = await _get_cursor(session, user.id, "hourly")
        last_id = cursor.last_message_id or 0
        pending = (await session.execute(
            select(Message)
            .join(Channel, Message.channel_id == Channel.id)
            .where(Message.tier == "hourly", Message.id > last_id, Message.merged_into_id.is_(None))
            .order_by(Message.id)
        )).scalars().all()

    if not pending:
        logger.info("Часовой батч: новых сообщений нет.")
        return

    logger.info("Часовой батч: обрабатываю %d сообщений.", len(pending))

    async with get_session() as session:
        context_text = await build_ai_context(session, user.id)
        meta = await _channel_meta(session)

    items = await _build_items(pending, meta, adapter)
    max_id = max(m.id for m in pending)

    try:
        important_ids = await classify_batch(context_text, items)
    except ClassifierError:
        # Курсор двигаем всё равно — иначе застрянем на этой пачке навсегда.
        await bot.send_message(
            user.tg_notify_chat_id,
            f"😳 Ой, ключи Gemini закончились — часовой разбор чатов не удался ({len(pending)} сообщений). "
            "Напиши скорее Никите, чтобы прислал новые (через команду /api), а пока стоит бегло проверить чаты самой.",
        )
        async with get_session() as session:
            cursor = await _get_cursor(session, user.id, "hourly")
            cursor.last_message_id = max_id
            await session.commit()
        return

    async with get_session() as session:
        now = datetime.utcnow()
        for m in pending:
            fresh = await session.get(Message, m.id)
            fresh.importance = m.id in important_ids
            fresh.classified_at = now
        cursor = await _get_cursor(session, user.id, "hourly")
        cursor.last_message_id = max_id
        await session.commit()

    await _notify_batch_results(bot, adapter, bot_id, user, pending, meta, important_ids, "📬 Часовая сводка")


async def run_hourly_scheduler(bot: Bot, adapter: TelegramAdapter, bot_id: int) -> None:
    while True:
        try:
            await _run_hourly_batch_once(bot, adapter, bot_id)
        except Exception:
            logger.exception("Необработанная ошибка в часовом джобе")

        # Настройки читаем заново каждый цикл — правки через /settings
        # применяются без перезапуска.
        async with get_session() as session:
            user = (await session.execute(select(User))).scalars().first()
        interval_minutes = user.hourly_interval_minutes if user and user.hourly_interval_minutes else 60
        await asyncio.sleep(interval_minutes * 60)


async def debug_full_analysis(bot: Bot, adapter: TelegramAdapter, bot_id: int) -> None:
    """Временная команда /fresh: прогоняет через ИИ все tier='hourly'
    сообщения, игнорируя курсор — повторяемо, для тестирования."""
    async with get_session() as session:
        user = (await session.execute(select(User))).scalars().first()
    if user is None:
        logger.info("/fresh: пользователя ещё нет.")
        return
    if user.tg_notify_chat_id is None:
        logger.info("/fresh: alert-бот ещё не подключён (/start там).")
        return

    async with get_session() as session:
        pending = (await session.execute(
            select(Message).where(Message.tier == "hourly", Message.merged_into_id.is_(None)).order_by(Message.id)
        )).scalars().all()

    if not pending:
        await bot.send_message(user.tg_notify_chat_id, "В БД пока нет сообщений с tier=hourly для анализа 🌸")
        return

    async with get_session() as session:
        context_text = await build_ai_context(session, user.id)
        meta = await _channel_meta(session)

    items = await _build_items(pending, meta, adapter)

    try:
        important_ids = await classify_batch(context_text, items)
    except ClassifierError as exc:
        await bot.send_message(user.tg_notify_chat_id, f"⚠️ Тестовый прогон не удался: {exc}")
        return

    async with get_session() as session:
        now = datetime.utcnow()
        for m in pending:
            fresh = await session.get(Message, m.id)
            fresh.importance = m.id in important_ids
            fresh.classified_at = now
        await session.commit()

    await _notify_batch_results(
        bot, adapter, bot_id, user, pending, meta, important_ids, "🧪 Тестовый полный анализ (/fresh)"
    )
