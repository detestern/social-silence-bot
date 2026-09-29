"""
Уведомление о важном сообщении — всегда ОДНО сообщение: шапка (время/чат/
отправитель) + текст, и если у оригинала было вложение (фото/файл/видео) —
оно же, приложенное сюда как caption, а не пересылкой отдельным сообщением.

Раньше для обычных групп слали шапку и следом пересылали оригинал через
Telethon — получалось два сообщения подряд, и по прямой просьбе (слишком
много уведомлений) от этого отказались: пересылки/форварда больше нет
нигде, только один send_* с текстом и (если есть) вложением.

Для супергрупп/каналов, где существует прямая ссылка на сообщение,
по-прежнему добавляем кнопку — она не создаёт второго сообщения, просто
кнопка под тем же самым.
"""
import logging
import mimetypes

from aiogram import Bot
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from adapters.telegram import TelegramAdapter

logger = logging.getLogger(__name__)

TEXT_LIMIT = 4096
CAPTION_LIMIT = 1024  # у Telegram caption для медиа короче обычного текста


def _jump_link(channel_external_id: str, message_id: str) -> str:
    internal_id = channel_external_id[4:] if channel_external_id.startswith("-100") else channel_external_id.lstrip("-")
    return f"https://t.me/c/{internal_id}/{message_id}"


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


async def notify_important(
    bot: Bot,
    adapter: TelegramAdapter,
    aiogram_chat_id: int,
    header_text: str,
    message_text: str,
    channel_external_id: str,
    channel_kind: str,
    message_external_id: str,
    has_media: bool = False,
) -> None:
    body = header_text if not message_text else f"{header_text}\n\n{message_text}"

    kb = None
    if channel_kind == "channel":
        link = _jump_link(channel_external_id, message_external_id)
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔗 Открыть в чате", url=link)]])

    media = None
    if has_media:
        try:
            media = await adapter.download_media_bytes(channel_external_id, int(message_external_id))
        except Exception:
            logger.exception(
                "Не удалось скачать вложение (chat=%s, msg=%s), шлю без него",
                channel_external_id, message_external_id,
            )

    if media is None:
        await bot.send_message(aiogram_chat_id, _truncate(body, TEXT_LIMIT), reply_markup=kb)
        return

    data, mime_type = media
    mime_type = mime_type or ""
    ext = mimetypes.guess_extension(mime_type) or ""
    filename = f"attachment{ext}"
    caption = _truncate(body, CAPTION_LIMIT)
    file = BufferedInputFile(data, filename=filename)

    try:
        if mime_type.startswith("image/"):
            await bot.send_photo(aiogram_chat_id, file, caption=caption, reply_markup=kb)
        elif mime_type.startswith("video/"):
            await bot.send_video(aiogram_chat_id, file, caption=caption, reply_markup=kb)
        elif mime_type.startswith("audio/"):
            await bot.send_audio(aiogram_chat_id, file, caption=caption, reply_markup=kb)
        else:
            await bot.send_document(aiogram_chat_id, file, caption=caption, reply_markup=kb)
    except Exception:
        logger.exception(
            "Не удалось отправить вложение одним сообщением (chat=%s, msg=%s), шлю текстом",
            channel_external_id, message_external_id,
        )
        await bot.send_message(aiogram_chat_id, _truncate(body, TEXT_LIMIT), reply_markup=kb)
