"""
Дневная сводка — не поиск важного, а беглый пересказ дня по каждому чату:
однотипные реплики (да/нет/буду) модель схлопывает в одну фразу.

Курсор 'daily' отдельный от 'hourly' — одно и то же сообщение спокойно
проходит через оба.
"""
import asyncio
import json
import logging
from datetime import timedelta

from aiogram import Bot
from sqlalchemy import select

from adapters.telegram import TelegramAdapter
from core.classifier import ClassifierError, _call_model, _strip_code_fences
from core.timeutil import local_now
from db.models import Channel, Message, ProcessingCursor, User
from db.session import get_session

logger = logging.getLogger(__name__)

# Дефолт, если у пользователя не проставлено (не должно случаться —
# в модели есть default=21). Реальное значение — user.daily_digest_hour,
# правится через /settings.
DEFAULT_DIGEST_HOUR = 21
# Защита от аномального объёма в одном чате.
MAX_MESSAGES_PER_CHANNEL = 400


async def _seconds_until_next_run(hour: int) -> float:
    now = local_now()
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


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


def _build_prompt(blocks: list[str]) -> str:
    return f"""Ниже переписка за сегодня, сгруппированная по чатам (каждый
блок помечен тегом вида [C1]). Для каждого чата напиши КОРОТКИЙ пересказ —
1-2 предложения о том, что там обсуждали, простым языком, в третьем лице.
Однотипные короткие реплики (да/нет/буду/+1 и т.п.) схлопни в одну общую
фразу — например "Все подтвердили участие, кроме Вани — он не сможет".
Если в чате не было ничего мало-мальски интересного (пустой обмен репликами
без темы, чистый шум) — пропусти этот чат вообще, не включай его в ответ.

{chr(10).join(blocks)}

Ответь СТРОГО в формате JSON, без пояснений и markdown:
{{"summaries": [{{"tag": "C1", "summary": "..."}}]}}"""


async def _run_daily_digest_once(bot: Bot, respect_cursor: bool = True) -> None:
    async with get_session() as session:
        user = (await session.execute(select(User))).scalars().first()
    if user is None or user.tg_notify_chat_id is None:
        logger.info("Дневная сводка: пользователь/alert-бот ещё не настроены, пропускаю.")
        return

    async with get_session() as session:
        last_id = 0
        if respect_cursor:
            cursor = await _get_cursor(session, user.id, "daily")
            last_id = cursor.last_message_id or 0
        pending = (await session.execute(
            select(Message).where(Message.id > last_id, Message.merged_into_id.is_(None)).order_by(Message.id)
        )).scalars().all()

    if not pending:
        await bot.send_message(user.tg_notify_chat_id, "📋 Сегодня новых сообщений не было 🌸")
        return

    async with get_session() as session:
        channels = {c.id: (c.title or "?") for c in (await session.execute(select(Channel))).scalars().all()}

    by_channel: dict[int, list[Message]] = {}
    for m in pending:
        by_channel.setdefault(m.channel_id, []).append(m)

    tag_map: dict[str, int] = {}
    blocks = []
    for idx, (channel_id, msgs) in enumerate(by_channel.items(), start=1):
        tag = f"C{idx}"
        tag_map[tag] = channel_id
        title = channels.get(channel_id, "?")
        trimmed = msgs[-MAX_MESSAGES_PER_CHANNEL:]
        lines = "\n".join(f"{m.sender_name or '?'}: {m.text or ''}" for m in trimmed)
        blocks.append(f"[{tag}] Чат: {title}\n{lines}")

    max_id = max(m.id for m in pending)

    try:
        raw = _strip_code_fences(await _call_model(_build_prompt(blocks)))
        data = json.loads(raw)
    except (ClassifierError, ValueError):
        logger.exception("Дневная сводка: ошибка ИИ")
        await bot.send_message(
            user.tg_notify_chat_id,
            "😳 Ой, ключи Gemini закончились — не смогла составить дневную сводку. "
            "Напиши скорее Никите, чтобы прислал новые (через команду /api)."
        )
        if respect_cursor:
            async with get_session() as session:
                cursor = await _get_cursor(session, user.id, "daily")
                cursor.last_message_id = max_id
                await session.commit()
        return

    if respect_cursor:
        async with get_session() as session:
            cursor = await _get_cursor(session, user.id, "daily")
            cursor.last_message_id = max_id
            await session.commit()

    summaries = data.get("summaries", [])
    if not summaries:
        await bot.send_message(user.tg_notify_chat_id, "📋 Сегодня был спокойный день, ничего особенного 🌸")
        return

    lines = []
    for item in summaries:
        title = channels.get(tag_map.get(item.get("tag")), "?")
        lines.append(f"• {title}: {item.get('summary', '')}")

    await bot.send_message(user.tg_notify_chat_id, "📋 Дневная сводка:\n\n" + "\n\n".join(lines))


async def run_daily_digest_scheduler(bot: Bot, adapter: TelegramAdapter, bot_id: int) -> None:
    while True:
        async with get_session() as session:
            user = (await session.execute(select(User))).scalars().first()
        hour = user.daily_digest_hour if user and user.daily_digest_hour is not None else DEFAULT_DIGEST_HOUR

        wait = await _seconds_until_next_run(hour)
        logger.info("Дневная сводка: следующий запуск через %.0f с. (в %d:00)", wait, hour)
        await asyncio.sleep(wait)

        # Час и включённость перечитываем перед каждым запуском — могли
        # поменяться через /settings, пока спали.
        async with get_session() as session:
            user = (await session.execute(select(User))).scalars().first()
        if user and not user.daily_digest_enabled:
            logger.info("Дневная сводка выключена в /settings, пропускаю сегодня.")
            continue

        try:
            await _run_daily_digest_once(bot)
        except Exception:
            logger.exception("Необработанная ошибка в дневной сводке")


async def debug_daily_digest_now(bot: Bot) -> None:
    """Временная команда /daily: составляет сводку прямо сейчас по всем
    сообщениям, игнорируя курсор — для тестирования, без порчи расписания."""
    await _run_daily_digest_once(bot, respect_cursor=False)
