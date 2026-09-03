"""
Схема БД под несколько источников (Telegram, Пачка, Gmail, Яндекс.Почта):
адаптеры переводят специфику платформы в одинаковые строки этих таблиц,
дальше весь пайплайн работает одинаково независимо от источника.
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger, Boolean, DateTime, ForeignKey, Integer, JSON, String, Text,
    UniqueConstraint, func
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Source(Base):
    """Справочник источников. Заполняется один раз при инициализации БД."""
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    # 'telegram' | 'pachca' | 'gmail' | 'yandex_mail'
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    channels: Mapped[list["Channel"]] = relationship(back_populates="source")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    display_name: Mapped[str] = mapped_column(String(128))
    # Куда бот шлёт уведомления — chat_id её чата с ALERT-ботом (не settings!).
    # NULL, пока не сделан /start в alert-боте.
    tg_notify_chat_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)

    # Правится через /settings, применяется на лету без перезапуска.
    hourly_interval_minutes: Mapped[int] = mapped_column(Integer, default=60)
    daily_digest_hour: Mapped[int] = mapped_column(Integer, default=21)  # час по её TIMEZONE
    daily_digest_enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    channels: Mapped[list["Channel"]] = relationship(back_populates="user")
    priority_rules: Mapped[list["PriorityRule"]] = relationship(back_populates="user")


class Channel(Base):
    """Обобщение "источника сообщений" внутри платформы: группа/канал/личка
    в TG, чат в Пачке, ящик/лейбл в почте.

    is_monitored — пока False, сообщения из этого канала не читаются вообще
    (фильтруется в адаптере ДО пайплайна, не постфактум).
    """
    __tablename__ = "channels"
    __table_args__ = (UniqueConstraint("source_id", "external_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))

    external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[Optional[str]] = mapped_column(String(256))
    kind: Mapped[Optional[str]] = mapped_column(String(32))
    # 'group' | 'channel' | 'dm' | 'mailbox' | 'thread'

    is_monitored: Mapped[bool] = mapped_column(Boolean, default=False)
    # Тег, который можно поставить чату один раз ("Школа №5"), чтобы
    # адресовать правила не конкретным чатам, а всей группе.
    group_label: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    discovered_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    user: Mapped["User"] = relationship(back_populates="channels")
    source: Mapped["Source"] = relationship(back_populates="channels")
    messages: Mapped[list["Message"]] = relationship(back_populates="channel")


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (UniqueConstraint("channel_id", "external_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"))

    external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    sender_name: Mapped[Optional[str]] = mapped_column(String(256))
    text: Mapped[Optional[str]] = mapped_column(Text)

    is_reply_to_user: Mapped[bool] = mapped_column(Boolean, default=False)
    is_direct_mention: Mapped[bool] = mapped_column(Boolean, default=False)
    has_media: Mapped[bool] = mapped_column(Boolean, default=False)

    sent_at: Mapped[datetime] = mapped_column(DateTime)

    tier: Mapped[str] = mapped_column(String(16), default="pending")
    # 'instant' | 'escalated' | 'hourly' | 'ignored' | 'merged' (обрывок,
    # влитый в другое сообщение через merged_into_id)
    importance: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    classified_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    merged_into_id: Mapped[Optional[int]] = mapped_column(ForeignKey("messages.id"), nullable=True)

    raw_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    channel: Mapped["Channel"] = relationship(back_populates="messages")


class Profile(Base):
    """Устаревшая модель — один общий блок текста без деления по группам.
    Оставлена только для миграции старых данных в ProfileSection (см.
    db/session.py); новый код её больше не пишет."""
    __tablename__ = "profile"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    text: Mapped[str] = mapped_column(Text, default="")


class ProfileSection(Base):
    """Общий профиль пользователя — один блок текста (channel_group всегда
    NULL). Оставлена как таблица с этой структурой ради простоты миграции;
    по сути используется как одна строка на пользователя."""
    __tablename__ = "profile_sections"
    __table_args__ = (UniqueConstraint("user_id", "channel_group"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    channel_group: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    text: Mapped[str] = mapped_column(Text, default="")


class School(Base):
    """Школа — замена тегов: и группа чатов (Channel.group_label == name),
    и область действия правил (PriorityRule.channel_group == name), и
    контейнер под свой скрытый профиль. computed_profile — то, что реально
    уходит в промпт для сообщений из этой школы: результат прогона общего
    /profile через ИИ с инструкцией "оставь общее + то, что про эту школу".
    Пересчитывается при создании школы и при каждом изменении /profile."""
    __tablename__ = "schools"
    __table_args__ = (UniqueConstraint("user_id", "name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(128))
    computed_profile: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    computed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class PriorityRule(Base):
    __tablename__ = "priority_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    source_id: Mapped[Optional[int]] = mapped_column(ForeignKey("sources.id"), nullable=True)  # NULL = все источники
    channel_group: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)  # NULL = все группы

    text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    user: Mapped["User"] = relationship(back_populates="priority_rules")


class ProcessingCursor(Base):
    __tablename__ = "processing_cursors"
    __table_args__ = (UniqueConstraint("user_id", "cursor_type"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    cursor_type: Mapped[str] = mapped_column(String(16))  # 'hourly' | 'daily'
    last_message_id: Mapped[Optional[int]] = mapped_column(ForeignKey("messages.id"), nullable=True)
