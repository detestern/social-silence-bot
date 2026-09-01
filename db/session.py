import os

from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from db.models import Base, Source

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


def get_session() -> AsyncSession:
    return async_session()
