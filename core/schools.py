"""
Пересчитывает School.computed_profile — скрытый, отфильтрованный под
конкретную школу вариант общего /profile. Вызывается при создании школы и
при каждом изменении /profile (все школы разом).
"""
import logging
from datetime import datetime

from sqlalchemy import select

from core.classifier import ClassifierError, extract_school_profile
from db.models import ProfileSection, School
from db.session import get_session

logger = logging.getLogger(__name__)


async def _get_base_profile_text(session, user_id: int) -> str:
    section = (await session.execute(
        select(ProfileSection).where(ProfileSection.user_id == user_id, ProfileSection.channel_group.is_(None))
    )).scalar_one_or_none()
    return section.text.strip() if section and section.text else ""


async def recompute_school_profile(school_id: int) -> bool:
    """Пересчитывает одну школу. Возвращает True при успехе."""
    async with get_session() as session:
        school = await session.get(School, school_id)
        if school is None:
            return False
        base_text = await _get_base_profile_text(session, school.user_id)

    if not base_text:
        # Профиль ещё не заполнен — пересчитывать нечего, оставляем как есть.
        return False

    try:
        computed = await extract_school_profile(base_text, school.name)
    except ClassifierError:
        logger.exception("Не удалось пересчитать профиль школы %s", school.name)
        return False

    async with get_session() as session:
        fresh = await session.get(School, school_id)
        fresh.computed_profile = computed
        fresh.computed_at = datetime.utcnow()
        await session.commit()
    return True


async def recompute_all_schools(user_id: int) -> None:
    """Вызывается после изменения /profile — пересчитывает все школы этого
    пользователя разом (по одному запросу к ИИ на школу)."""
    async with get_session() as session:
        schools = (await session.execute(select(School).where(School.user_id == user_id))).scalars().all()
    for school in schools:
        await recompute_school_profile(school.id)
