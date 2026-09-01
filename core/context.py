"""
Собирает "кто она и что для неё важно" из profile + priority_rules для
промпта классификатора. Все активные правила идут разом с пометкой области
действия в скобках — модель сама сопоставляет с группой чата сообщения,
так один батч-запрос на разные чаты остаётся одним запросом.
"""
from datetime import datetime, timezone

from sqlalchemy import select

from db.models import PriorityRule, Profile, Source


async def _active_rules_text(session, user_id: int) -> str:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    rules = (await session.execute(
        select(PriorityRule).where(
            PriorityRule.user_id == user_id,
            PriorityRule.is_active == True,  # noqa: E712
        )
    )).scalars().all()

    active = [r for r in rules if r.expires_at is None or r.expires_at > now]
    if not active:
        return ""

    sources = {s.id: s.code for s in (await session.execute(select(Source))).scalars().all()}

    lines = []
    for r in active:
        scope_bits = []
        if r.source_id is not None:
            scope_bits.append(f"источник: {sources.get(r.source_id, '?')}")
        if r.channel_group:
            scope_bits.append(f"группа чатов: «{r.channel_group}»")
        scope = f" ({', '.join(scope_bits)})" if scope_bits else " (для всех чатов)"
        lines.append(f"- {r.text}{scope}")

    return "\n".join(lines)


async def build_ai_context(session, user_id: int) -> str:
    profile = await session.get(Profile, user_id)
    profile_text = profile.text.strip() if profile and profile.text else ""

    parts = []
    if profile_text:
        parts.append(f"О пользователе:\n{profile_text}")
    else:
        parts.append(
            "О пользователе: профиль пока не заполнен. Считай важным всё, что "
            "выглядит адресованным лично получателю, срочным или требующим действия, "
            "и НЕважным общий шум/поздравления/оффтоп, не спрашивающий ни о чём конкретном."
        )

    rules_text = await _active_rules_text(session, user_id)
    if rules_text:
        parts.append(
            "Текущие приоритетные правила (сообщения по этим темам — точно важны, "
            "но только если область действия правила подходит к чату сообщения):\n"
            + rules_text
        )

    return "\n\n".join(parts)
