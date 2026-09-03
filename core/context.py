"""
Собирает контекст для промпта классификатора. В отличие от старой схемы,
теперь это делается ОТДЕЛЬНО под каждую школу (или под "без школы"):
для сообщения из чата школы «Летово» берётся её computed_profile (уже
отфильтрованный от других школ) + правила, применимые к «Летово» или ко
всем школам разом. Для чата без школы — общий /profile напрямую.
"""
from datetime import datetime, timezone

from sqlalchemy import select

from db.models import PriorityRule, ProfileSection, School


async def _general_profile_block(session, user_id: int) -> str:
    section = (await session.execute(
        select(ProfileSection).where(ProfileSection.user_id == user_id, ProfileSection.channel_group.is_(None))
    )).scalar_one_or_none()
    text = section.text.strip() if section and section.text else ""
    if text:
        return f"О пользователе:\n{text}"
    return (
        "О пользователе: профиль пока не заполнен. Считай важным всё, что "
        "выглядит адресованным лично получателю, срочным или требующим действия, "
        "и НЕважным общий шум/поздравления/оффтоп, не спрашивающий ни о чём конкретном."
    )


async def _rules_text(session, user_id: int, school_name: str | None) -> str:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    rules = (await session.execute(
        select(PriorityRule).where(PriorityRule.user_id == user_id, PriorityRule.is_active == True)  # noqa: E712
    )).scalars().all()

    active = [
        r for r in rules
        if (r.expires_at is None or r.expires_at > now)
        and (r.channel_group is None or r.channel_group == school_name)
    ]
    if not active:
        return ""

    lines = []
    for r in active:
        scope = f" (школа: «{r.channel_group}»)" if r.channel_group else " (для всех школ)"
        lines.append(f"- {r.text}{scope}")
    return "\n".join(lines)


async def build_ai_context(session, user_id: int, school_name: str | None = None) -> str:
    if school_name:
        school = (await session.execute(
            select(School).where(School.user_id == user_id, School.name == school_name)
        )).scalar_one_or_none()
        if school and school.computed_profile:
            profile_block = f"О пользователе (в контексте школы «{school_name}»):\n{school.computed_profile}"
        else:
            # Профиль школы ещё не посчитан (например, только что создали
            # школу) — безопасный откат на общий профиль, не хуже, чем было.
            profile_block = await _general_profile_block(session, user_id)
    else:
        profile_block = await _general_profile_block(session, user_id)

    parts = [profile_block]

    rules_text = await _rules_text(session, user_id, school_name)
    if rules_text:
        parts.append("Текущие приоритетные правила (сообщения по этим темам — точно важны):\n" + rules_text)

    return "\n\n".join(parts)
