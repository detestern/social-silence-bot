"""
Контракт адаптера источника (Telegram, Пачка, Gmail, Яндекс.Почта): всё
специфичное для платформы остаётся внутри адаптера, наружу отдаётся только
эта структура — дальше пайплайн про платформу вообще ничего не знает.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import AsyncIterator, Callable, Optional


@dataclass
class NormalizedChannel:
    """Чат/группа/ящик, обнаруженный у пользователя на платформе.
    Используется на этапе "выбери, что мониторить"."""
    external_id: str
    title: str
    kind: str  # 'group' | 'channel' | 'dm' | 'mailbox' | 'thread'


@dataclass
class NormalizedMessage:
    channel_external_id: str
    external_id: str
    sender_name: Optional[str]
    sender_id: Optional[int]
    text: str
    is_reply_to_user: bool
    is_direct_mention: bool
    has_media: bool
    sent_at: datetime
    raw: dict


class SourceAdapter(ABC):
    source_code: str

    @abstractmethod
    async def discover_channels(self) -> list[NormalizedChannel]:
        """Все доступные чаты/ящики пользователя — для UI выбора мониторинга."""
        ...

    @abstractmethod
    async def listen(self, get_monitored_ids: Callable[[], set[str]]) -> AsyncIterator[NormalizedMessage]:
        """Отдаёт только сообщения из чатов в get_monitored_ids() — это
        функция (не статичный set), читается на каждое сообщение, чтобы
        /chats применялся без перезапуска. Немониторимые чаты отбрасываются
        до сохранения куда-либо."""
        ...
