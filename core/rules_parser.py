"""
Превращает её фразу ("у нас экскурсия, я отвечаю за 10Б") в структурированную
заготовку правила (текст + срок в днях) — не мёржит в промпт напрямую,
финальный промпт собирается кодом из отдельных строк (core/context.py).
"""
import json
import logging
from typing import Optional, TypedDict

from core.classifier import _call_model, _strip_code_fences

logger = logging.getLogger(__name__)


class ParsedRule(TypedDict):
    text: str
    expires_in_days: Optional[int]


async def parse_rule_text(raw_text: str) -> ParsedRule:
    prompt = f"""Приведи фразу пользователя к короткому чёткому правилу
приоритета для фильтра сообщений (третье лицо, без "я"/"мне", по-деловому).
Если во фразе явно подразумевается временный срок действия (например
"на этой неделе", "до конца месяца", "на две недели") — укажи его в днях,
округляя разумно. Если срок не подразумевается — верни null.

Фраза: "{raw_text}"

Ответь СТРОГО в формате JSON, без пояснений и без markdown-разметки:
{{"text": "...", "expires_in_days": 14}}
или
{{"text": "...", "expires_in_days": null}}"""

    try:
        raw = _strip_code_fences(await _call_model(prompt))
        data = json.loads(raw)
        return ParsedRule(
            text=str(data.get("text") or raw_text).strip(),
            expires_in_days=data.get("expires_in_days"),
        )
    except Exception:
        logger.exception("Не удалось распарсить правило через ИИ — использую текст как есть")
        return ParsedRule(text=raw_text.strip(), expires_in_days=None)
