import os

from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from db.models import Base, Channel, PriorityRule, Profile, ProfileSection, School, Source

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./triage.db")

engine = create_async_engine(DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, expire_on_commit=False)

SOURCE_CODES = ["telegram", "pachca", "gmail", "yandex_mail"]


if DATABASE_URL.startswith("sqlite"):
    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, connection_record):
        """WAL — чтобы несколько соединений (боты, слушатель, джобы) могли
        читать и писать одновременно без 'database is locked'."""
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()


async def _migrate_legacy_profile(session) -> None:
    """Разовый перенос: старый Profile.text (один блок на пользователя) —
    в новую ProfileSection с channel_group=NULL (общая часть). Не трогает
    ничего, если в новой таблице уже есть хоть одна запись — значит,
    перенос уже случился или профиль уже набирается по-новому."""
    has_new_data = (await session.execute(select(ProfileSection.id))).first()
    if has_new_data:
        return
    legacy_rows = (await session.execute(select(Profile))).scalars().all()
    migrated = False
    for row in legacy_rows:
        if row.text and row.text.strip():
            session.add(ProfileSection(user_id=row.user_id, channel_group=None, text=row.text))
            migrated = True
    if migrated:
        await session.commit()


async def _migrate_tags_to_schools(session) -> None:
    """Если она уже успела создать теги через /tag до этого обновления —
    превращаем каждое встреченное имя тега в полноценную школу (с пустым
    computed_profile — он посчитается при следующем /profile). Ничего не
    трогает у школ, которые уже существуют с таким же именем."""
    names_by_user: dict[int, set[str]] = {}

    channels = (await session.execute(select(Channel).where(Channel.group_label.is_not(None)))).scalars().all()
    for c in channels:
        names_by_user.setdefault(c.user_id, set()).add(c.group_label)

    rules = (await session.execute(select(PriorityRule).where(PriorityRule.channel_group.is_not(None)))).scalars().all()
    for r in rules:
        names_by_user.setdefault(r.user_id, set()).add(r.channel_group)

    if not names_by_user:
        return

    for user_id, names in names_by_user.items():
        existing = (await session.execute(
            select(School.name).where(School.user_id == user_id)
        )).scalars().all()
        existing_set = set(existing)
        for name in names:
            if name not in existing_set:
                session.add(School(user_id=user_id, name=name, computed_profile=None))
    await session.commit()


async def init_db() -> None:
    """Создаёт таблицы (если их ещё нет) и досеивает недостающие источники.
    Безопасно вызывать при каждом старте приложения."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with async_session() as session:
        existing = (await session.execute(select(Source.code))).scalars().all()
        missing = [code for code in SOURCE_CODES if code not in existing]
        for code in missing:
            session.add(Source(code=code, is_enabled=(code == "telegram")))
        if missing:
            await session.commit()

    async with async_session() as session:
        await _migrate_legacy_profile(session)

    async with async_session() as session:
        await _migrate_tags_to_schools(session)


def get_session() -> AsyncSession:
    return async_session()
