"""
Опрашивает Yandex.Почту (см. adapters/yandex_mail.py) и сохраняет письма
как обычные Message с tier='hourly' — дальше их разбирает уже существующий
core/scheduler.py (часовой батч) и core/digest.py (сводка), им всё равно,
откуда сообщение взялось. Отдельной эскалации/instant для почты пока нет —
письма по природе менее срочные, чем чаты.
"""
import asyncio
import logging

from sqlalchemy import select

from adapters.yandex_mail import YandexMailAdapter
from db.models import Channel, Message, Source, User
from db.session import get_session

logger = logging.getLogger(__name__)

REFRESH_INTERVAL_SECONDS = 30
USER_WAIT_RETRY_SECONDS = 10


class _MonitoredFoldersCache:
    def __init__(self) -> None:
        self._channel_map: dict[str, int] = {}

    def ids(self) -> set[str]:
        return set(self._channel_map.keys())

    def channel_id(self, folder: str) -> int | None:
        return self._channel_map.get(folder)

    async def refresh_once(self) -> None:
        async with get_session() as session:
            source = (await session.execute(select(Source).where(Source.code == "yandex_mail"))).scalar_one_or_none()
            if source is None:
                return
            rows = (await session.execute(
                select(Channel).where(Channel.source_id == source.id, Channel.is_monitored == True)  # noqa: E712
            )).scalars().all()
        self._channel_map = {c.external_id: c.id for c in rows}

    async def refresh_loop(self) -> None:
        while True:
            try:
                await self.refresh_once()
            except Exception:
                logger.exception("Не удалось обновить список мониторимых папок почты")
            await asyncio.sleep(REFRESH_INTERVAL_SECONDS)


async def _wait_for_ready_user() -> User:
    logged_once = False
    while True:
        async with get_session() as session:
            user = (await session.execute(select(User))).scalars().first()
        if user is not None and user.tg_notify_chat_id is not None:
            return user
        if not logged_once:
            logger.info("Почта: жду пользователя с настроенным alert-ботом.")
            logged_once = True
        await asyncio.sleep(USER_WAIT_RETRY_SECONDS)


async def run_mail_listener(adapter: YandexMailAdapter) -> None:
    cache = _MonitoredFoldersCache()
    await cache.refresh_once()
    refresh_task = asyncio.create_task(cache.refresh_loop())

    await _wait_for_ready_user()
    logger.info("Слушатель Yandex.Почты запущен, мониторится папок: %d", len(cache.ids()))

    try:
        async for msg in adapter.listen(cache.ids):
            channel_id = cache.channel_id(msg.channel_external_id)
            if channel_id is None:
                continue  # папку сняли с мониторинга буквально между опросом и обработкой

            async with get_session() as session:
                existing = (await session.execute(
                    select(Message).where(Message.channel_id == channel_id, Message.external_id == msg.external_id)
                )).scalar_one_or_none()
                if existing:
                    continue

                sent_at = msg.sent_at.replace(tzinfo=None) if msg.sent_at.tzinfo else msg.sent_at
                session.add(Message(
                    channel_id=channel_id,
                    external_id=msg.external_id,
                    sender_name=msg.sender_name,
                    text=msg.text,
                    is_reply_to_user=False,
                    is_direct_mention=False,
                    has_media=False,
                    sent_at=sent_at,
                    tier="hourly",
                    raw_json=msg.raw,
                ))
                await session.commit()

            logger.info("Сохранено письмо из папки %s", msg.channel_external_id)
    finally:
        refresh_task.cancel()
        await asyncio.gather(refresh_task, return_exceptions=True)
