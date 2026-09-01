from typing import Callable, Optional

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

PAGE_SIZE = 8

# Режим и текст фильтра/тега хранятся в bot.handlers._ACTIVE_VIEW по
# chat_id, не в callback_data — у Telegram лимит 64 байта, длинный тег в
# base64 туда не влезал. Режимы: 'a' весь список, 'm' только мониторимые,
# 'f' результат /find, 'g' тегирование (/tag).


def channels_keyboard(
    channels: list,
    page: int,
    finish_label: str = "✔️ Готово",
    is_checked: Optional[Callable[[object], bool]] = None,
) -> InlineKeyboardMarkup:
    """channels — отфильтрованный список Channel. is_checked — чем считать
    чат отмеченным (по умолчанию is_monitored; для 'g' — group_label==query).
    callback_data несёт только id чата и страницу, режим достаётся из
    _ACTIVE_VIEW по chat_id."""
    is_checked = is_checked or (lambda c: c.is_monitored)
    start = page * PAGE_SIZE
    chunk = channels[start:start + PAGE_SIZE]

    rows = []
    for ch in chunk:
        mark = "✅" if is_checked(ch) else "⬜"
        title = ch.title[:40] if ch.title else ch.external_id
        rows.append([
            InlineKeyboardButton(text=f"{mark} {title}", callback_data=f"toggle:{ch.id}:{page}")
        ])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"page:{page - 1}"))
    total_pages = (len(channels) - 1) // PAGE_SIZE + 1 if channels else 1
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="noop"))
    if start + PAGE_SIZE < len(channels):
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"page:{page + 1}"))
    if nav:
        rows.append(nav)

    rows.append([InlineKeyboardButton(text=finish_label, callback_data="done")])

    return InlineKeyboardMarkup(inline_keyboard=rows)
