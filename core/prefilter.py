"""
Три уровня приоритизации сообщения без единого обращения к ИИ:

  instant   — 100% надёжные структурные сигналы: реплай ей, прямое
              упоминание, личка. Тут ИИ не нужен вообще.

  escalated — есть временной ("сегодня", "15:00") или срочный маркер
              ("срочно", "замена"), либо есть вложение (файл/фото могут
              быть важны без единого слова текста). Это не финальный
              вердикт, а кандидат на внеочередной одиночный запрос к ИИ.

  hourly    — всё остальное, копится и уходит батчем.
"""
import re

from adapters.base import NormalizedMessage

TIME_SIGNAL_RE = re.compile(
    r"\bсегодня\b|\bзавтра\b|\bсейчас\b|\bчерез\s+\d+\s*(минут|мин|час)|\b\d{1,2}:\d{2}\b",
    re.IGNORECASE,
)

URGENT_KEYWORDS = ("срочно", "замена", "отменяется", "отменили", "внимание")


def classify_tier(msg: NormalizedMessage, channel_kind: str) -> str:
    if msg.is_reply_to_user or msg.is_direct_mention or channel_kind == "dm":
        return "instant"

    if msg.has_media:
        return "escalated"

    text_lower = (msg.text or "").lower()
    has_time_signal = bool(TIME_SIGNAL_RE.search(text_lower))
    has_urgent_kw = any(kw in text_lower for kw in URGENT_KEYWORDS)

    return "escalated" if (has_time_signal or has_urgent_kw) else "hourly"
