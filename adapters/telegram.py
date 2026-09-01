import asyncio
import logging
import os
import re
from typing import AsyncIterator, Callable, Optional

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.utils import get_peer_id

from adapters.base import NormalizedChannel, NormalizedMessage, SourceAdapter

logger = logging.getLogger(__name__)

# t.me/c/1234567890/456 — приватная супергруппа/канал, число после /c/ — внутренний id без -100
_PRIVATE_LINK_RE = re.compile(r"t\.me/c/(\d+)/(\d+)")
# t.me/username/456 — публичный чат/канал
_PUBLIC_LINK_RE = re.compile(r"t\.me/([A-Za-z][A-Za-z0-9_]{3,31})/(\d+)")


def _dialog_kind(dialog) -> str:
    # is_group у Telethon истинно и для базовых групп, и для супергрупп —
    # а ссылки на сообщение (t.me/c/...) поддерживают только супергруппы и
    # каналы, технически это одно и то же (Channel). Проверяем is_channel
    # первым, иначе супергруппы ошибочно попадают в 'group'.
    if dialog.is_channel:
        return "channel"
    if dialog.is_group:
        return "group"
    return "dm"


class TelegramAdapter(SourceAdapter):
    source_code = "telegram"

    MAX_MEDIA_BYTES = 15 * 1024 * 1024

    def __init__(self) -> None:
        api_id = int(os.environ["TG_API_ID"])
        api_hash = os.environ["TG_API_HASH"]
        session_name = os.getenv("TG_SESSION_NAME", "triage_userbot")
        self.client = TelegramClient(session_name, api_id, api_hash)
        self._me_id: Optional[int] = None  # id владельца сессии — нужен для "реплай именно ей"

    async def discover_channels(self) -> list[NormalizedChannel]:
        await self.client.connect()
        if not await self.client.is_user_authorized():
            raise RuntimeError("Сессия ещё не авторизована — сначала набери /login.")
        result = []
        async for dialog in self.client.iter_dialogs():
            result.append(
                NormalizedChannel(
                    external_id=str(dialog.id),
                    title=dialog.name or "(без названия)",
                    kind=_dialog_kind(dialog),
                )
            )
        return result

    async def resolve_link(self, link: str) -> Optional[NormalizedChannel]:
        """Разбирает ссылку на сообщение (t.me/c/... или t.me/username/...)."""
        await self.client.connect()

        private_match = _PRIVATE_LINK_RE.search(link)
        if private_match:
            internal_id = private_match.group(1)
            external_id = f"-100{internal_id}"
            try:
                entity = await self.client.get_entity(int(external_id))
            except (ValueError, TypeError):
                return None
            title = getattr(entity, "title", None) or getattr(entity, "first_name", None)
            return NormalizedChannel(
                external_id=str(get_peer_id(entity)),
                title=title or "(без названия)",
                kind="channel" if getattr(entity, "broadcast", False) else "group",
            )

        public_match = _PUBLIC_LINK_RE.search(link)
        if public_match:
            username = public_match.group(1)
            try:
                entity = await self.client.get_entity(username)
            except ValueError:
                return None
            title = getattr(entity, "title", None) or getattr(entity, "first_name", None)
            return NormalizedChannel(
                external_id=str(get_peer_id(entity)),
                title=title or "(без названия)",
                kind="channel" if getattr(entity, "broadcast", False) else "group",
            )

        return None

    async def forward_message(self, from_channel_external_id: str, message_id: int, to_peer) -> None:
        """Пересылает сообщение в to_peer через её же аккаунт — работает и
        для обычных групп, где прямых ссылок не существует."""
        await self.client.connect()
        for attempt in range(2):
            try:
                await self.client.forward_messages(
                    entity=to_peer, messages=message_id, from_peer=int(from_channel_external_id),
                )
                return
            except FloodWaitError as exc:
                if attempt == 0:
                    logger.warning("FloodWait при пересылке, жду %d с.", exc.seconds)
                    await asyncio.sleep(exc.seconds + 1)
                else:
                    raise

    async def download_media_bytes(self, channel_external_id: str, message_id: int) -> Optional[tuple]:
        """Докачивает вложение для мультимодального анализа — лениво, в
        момент реального похода в ИИ, а не при получении события."""
        await self.client.connect()
        try:
            msg = await self.client.get_messages(int(channel_external_id), ids=int(message_id))
        except Exception:
            return None
        if msg is None or msg.media is None:
            return None
        size = getattr(msg.file, "size", None)
        if size and size > self.MAX_MEDIA_BYTES:
            return None

        data = None
        for attempt in range(2):
            try:
                data = await msg.download_media(file=bytes)
                break
            except FloodWaitError as exc:
                if attempt == 0:
                    logger.warning("FloodWait при скачивании, жду %d с.", exc.seconds)
                    await asyncio.sleep(exc.seconds + 1)
                else:
                    return None
            except Exception:
                return None
        if not data:
            return None
        mime_type = getattr(msg.file, "mime_type", None) or "application/octet-stream"
        return data, mime_type

    async def listen(self, get_monitored_ids: Callable[[], set[str]]) -> AsyncIterator[NormalizedMessage]:
        await self.client.connect()
        if self._me_id is None:
            me = await self.client.get_me()
            if me is None:
                raise RuntimeError("Telegram-сессия не авторизована — нужно /login")
            self._me_id = me.id

        queue: asyncio.Queue[NormalizedMessage] = asyncio.Queue()

        # Подписка без chats=... — фильтрация по мониторимым чатам вручную,
        # в начале хэндлера, чтобы /chats/tag применялись без перезапуска.
        @self.client.on(events.NewMessage())
        async def _handler(event):
            if str(event.chat_id) not in get_monitored_ids():
                return

            sender = await event.get_sender()
            sender_name = getattr(sender, "first_name", None) or getattr(sender, "title", None)

            is_mention = False
            if event.message.entities:
                for ent in event.message.entities:
                    if ent.__class__.__name__ in ("MessageEntityMention", "MessageEntityMentionName"):
                        is_mention = True
                        break

            # is_reply у Telethon значит "это вообще реплай", не "реплай ей" —
            # сверяем автора сообщения, на которое отвечают.
            is_reply_to_user = False
            if event.is_reply:
                reply_msg = await event.get_reply_message()
                if reply_msg is not None and reply_msg.sender_id == self._me_id:
                    is_reply_to_user = True

            has_media = event.message.media is not None
            text = event.message.message or ""

            if event.message.voice is not None:
                has_media = False
                try:
                    from core.classifier import ClassifierError, transcribe_audio
                    audio_bytes = await event.message.download_media(file=bytes)
                    mime_type = getattr(event.message.file, "mime_type", None) or "audio/ogg"
                    try:
                        text = await transcribe_audio(audio_bytes, mime_type)
                        if not text.strip():
                            text = "[Голосовое сообщение — распознанный текст пуст]"
                    except ClassifierError:
                        text = "[Голосовое сообщение — не удалось распознать: закончились ключи ИИ]"
                except Exception:
                    logger.exception("Не удалось скачать/распознать голосовое сообщение")
                    text = "[Голосовое сообщение — не удалось скачать]"
            elif event.message.sticker is not None or event.message.gif is not None:
                # Реакция, не информация — не эскалируем и не тратим ИИ.
                has_media = False
                text = "[Стикер]" if event.message.sticker is not None else "[GIF]"
            elif has_media and not text.strip():
                # Файл без подписи — подставляем имя как плейсхолдер, сам
                # файл докачается на этапе классификации.
                file_name = None
                doc = getattr(event.message, "document", None)
                if doc is not None:
                    for attr in getattr(doc, "attributes", []):
                        file_name = getattr(attr, "file_name", None)
                        if file_name:
                            break
                text = f"[Вложение без подписи: {file_name or 'файл/фото'}]"

            await queue.put(
                NormalizedMessage(
                    channel_external_id=str(event.chat_id),
                    external_id=str(event.message.id),
                    sender_name=sender_name,
                    sender_id=event.sender_id,
                    text=text,
                    is_reply_to_user=is_reply_to_user,
                    is_direct_mention=is_mention,
                    has_media=has_media,
                    sent_at=event.message.date,
                    raw={"message_id": event.message.id, "chat_id": event.chat_id},
                )
            )

        try:
            while True:
                msg = await queue.get()
                yield msg
        finally:
            # Снимаем обработчик — иначе при переподключении зарегистрируется
            # второй поверх первого, и сообщения задвоятся.
            self.client.remove_event_handler(_handler)
