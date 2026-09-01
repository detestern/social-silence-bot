"""
Уведомление о важном сообщении: для супергрупп/каналов — кнопка с прямой
ссылкой на сообщение, для обычных групп (ссылок для них не существует) —
пересылка оригинала через её же Telethon-аккаунт.
"""
import logging

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from adapters.telegram import TelegramAdapter

logger = logging.getLogger(__name__)


def _jump_link(channel_external_id: str, message_id: str) -> str:
    internal_id = channel_external_id[4:] if channel_external_id.startswith("-100") else channel_external_id.lstrip("-")
    return f"https://t.me/c/{internal_id}/{message_id}"


async def notify_important(
    bot: Bot,
    adapter: TelegramAdapter,
    aiogram_chat_id: int,
    forward_target_peer: int,
    header_text: str,
    channel_external_id: str,
    channel_kind: str,
    message_external_id: str,
) -> None:
    if channel_kind == "channel":
        link = _jump_link(channel_external_id, message_external_id)
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔗 Открыть в чате", url=link)]])
        await bot.send_message(aiogram_chat_id, header_text, reply_markup=kb)
        return

    await bot.send_message(aiogram_chat_id, header_text)
    try:
        await adapter.forward_message(channel_external_id, int(message_external_id), forward_target_peer)
    except Exception:
        logger.exception("Не удалось переслать сообщение (chat=%s, msg=%s)", channel_external_id, message_external_id)
