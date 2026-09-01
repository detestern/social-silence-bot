"""
Классификатор поверх Gemini API: classify_single (одно escalated-сообщение)
и classify_batch (пачка hourly-сообщений, ответ — только id важных).

Несколько ключей через запятую в GEMINI_API_KEY — крутятся по кругу,
упавший пропускается. add_extra_keys() добавляет резервные ключи на лету
(команда /api), сохраняя их в extra_gemini_keys.txt на случай перезапуска.
"""
import itertools
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

MODEL_NAME = "gemini-3.6-flash"
EXTRA_KEYS_PATH = Path(__file__).resolve().parent.parent / "extra_gemini_keys.txt"

_clients: list[genai.Client] = []
_keys: list[str] = []  # для проверки дублей при добавлении новых
_key_cycle = None  # инициализируется лениво/пересоздаётся при изменении _clients


def _rebuild_cycle() -> None:
    global _key_cycle
    _key_cycle = itertools.cycle(range(len(_clients)))


def _load_keys_from_disk() -> list[str]:
    raw_env = os.environ.get("GEMINI_API_KEY", "")
    keys = [k.strip() for k in raw_env.split(",") if k.strip()]
    if EXTRA_KEYS_PATH.exists():
        for line in EXTRA_KEYS_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                keys.append(line)
    return keys


def _ensure_clients() -> None:
    if _clients:
        return
    keys = _load_keys_from_disk()
    if not keys:
        raise RuntimeError("GEMINI_API_KEY пуст, и extra_gemini_keys.txt тоже пуст/отсутствует")
    for key in keys:
        _clients.append(genai.Client(api_key=key))
        _keys.append(key)
    _rebuild_cycle()
    logger.info("Gemini: загружено ключей — %d", len(_clients))


def add_extra_keys(new_keys: list[str]) -> int:
    """Добавляет ключи в ротацию сразу, без перезапуска, и сохраняет на
    диск. Возвращает число реально новых (не дублирующих) ключей."""
    _ensure_clients()
    added = 0
    with EXTRA_KEYS_PATH.open("a", encoding="utf-8") as f:
        for key in new_keys:
            key = key.strip()
            if not key or key in _keys:
                continue
            _clients.append(genai.Client(api_key=key))
            _keys.append(key)
            f.write(key + "\n")
            added += 1
    if added:
        _rebuild_cycle()
    return added


def _strip_code_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


@dataclass
class ClassifyBatchItem:
    id: int
    channel_title: str
    channel_group: Optional[str]
    sender_name: Optional[str]
    text: str
    media: Optional[tuple] = None  # (bytes, mime_type) — докачивается вызывающим кодом (scheduler.py)


class ClassifierError(Exception):
    """Поднимается, когда ВСЕ ключи по очереди не сработали — вызывающий
    код отличает так "не важно" от "не смогли спросить модель"."""


async def _call_model(contents: Union[str, list]) -> str:
    """Round-robin по ключам; contents — строка или список (текст вперемешку
    с types.Part.from_bytes(...) для вложений/аудио)."""
    _ensure_clients()
    n = len(_clients)
    start = next(_key_cycle)

    last_exc: Optional[Exception] = None
    for offset in range(n):
        idx = (start + offset) % n
        try:
            response = _clients[idx].models.generate_content(model=MODEL_NAME, contents=contents)
            return response.text
        except Exception as exc:
            logger.warning("Gemini-ключ #%d не сработал (%s), пробую следующий", idx + 1, exc)
            last_exc = exc
            continue

    raise ClassifierError(str(last_exc))


async def transcribe_audio(data: bytes, mime_type: str) -> str:
    """Расшифровывает голосовое сообщение через Gemini напрямую (без
    отдельного speech-to-text) — дальше текст идёт по обычному пайплайну."""
    contents = [
        "Расшифруй это голосовое сообщение в текст на русском языке. "
        "Верни ТОЛЬКО сам текст расшифровки, без пояснений, кавычек и markdown-разметки.",
        types.Part.from_bytes(data=data, mime_type=mime_type),
    ]
    raw = await _call_model(contents)
    return raw.strip()


async def classify_single(
    context_text: str,
    sender_name: Optional[str],
    channel_title: str,
    channel_group: Optional[str],
    text: str,
    media: Optional[tuple] = None,
) -> bool:
    group_note = f", группа чатов: «{channel_group}»" if channel_group else ""
    media_note = "\n\nК сообщению приложен файл/фото — он передан ниже, учти его содержимое при оценке." if media else ""
    prompt_text = f"""Ты — фильтр важности сообщений для рабочих Telegram-чатов.

{context_text}

Сообщение (отправитель: {sender_name or "неизвестно"}, чат: {channel_title}{group_note}):
"{text}"{media_note}

Некоторые из правил выше применяются только к определённой группе чатов
(указано у правила в скобках) — учитывай это: применяй такое правило,
только если группа чата у сообщения совпадает с группой правила. Правила
без указанной группы действуют на все чаты.

Это сообщение реально требует внимания получателя, или это фоновой шум
(поздравления, обсуждения не касающихся её классов/тем, общие
объявления не по делу)?

Ответь СТРОГО в формате JSON, без пояснений и без markdown-разметки:
{{"important": true}} или {{"important": false}}"""

    if media:
        data, mime_type = media
        contents: Union[str, list] = [prompt_text, types.Part.from_bytes(data=data, mime_type=mime_type)]
    else:
        contents = prompt_text

    raw = _strip_code_fences(await _call_model(contents))
    try:
        data_json = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ClassifierError(f"Не смог разобрать ответ ИИ: {raw!r}") from exc
    return bool(data_json.get("important", False))


async def classify_batch(context_text: str, items: list[ClassifyBatchItem]) -> set[int]:
    """Возвращает id важных сообщений (не полный вердикт на каждое — экономит
    токены). Без вложений — одна строка промпта; с вложениями — список
    частей (contents) с реальными байтами файла на своём месте."""
    if not items:
        return set()

    def _line(it: ClassifyBatchItem) -> str:
        group_note = f", группа: «{it.channel_group}»" if it.channel_group else ""
        media_note = " [приложен файл/фото, см. ниже]" if it.media else ""
        return f'[{it.id}] чат: "{it.channel_title}"{group_note}, от: {it.sender_name or "?"}: {it.text}{media_note}'

    header = f"""Ты — фильтр важности сообщений для рабочих Telegram-чатов.

{context_text}

Некоторые из правил выше применяются только к определённой группе чатов
(указано у правила в скобках), а у части сообщений ниже тоже указана
группа — применяй такое правило, только если группы совпадают. Правила
без указанной группы действуют на все чаты и сообщения независимо от их
группы.

Ниже — пачка сообщений за последний час, каждое с номером в квадратных
скобках. У некоторых есть приложенный файл/фото — он идёт сразу после
соответствующей строки, учти его содержимое при оценке. Верни номера
ТОЛЬКО тех сообщений, которые реально требуют внимания получателя (не
фоновой шум, не поздравления не по её темам, не общие объявления, её не
касающиеся).

Сообщения:
"""
    footer = """

Ответь СТРОГО в формате JSON, без пояснений и без markdown-разметки:
{"important_ids": [1, 5, 12]}
Если важных нет — {"important_ids": []}"""

    has_any_media = any(it.media for it in items)

    if not has_any_media:
        messages_block = "\n".join(_line(it) for it in items)
        contents: Union[str, list] = header + messages_block + footer
    else:
        parts: list = [header]
        for it in items:
            parts.append(_line(it))
            if it.media:
                data, mime_type = it.media
                parts.append(types.Part.from_bytes(data=data, mime_type=mime_type))
        parts.append(footer)
        contents = parts

    raw = _strip_code_fences(await _call_model(contents))
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ClassifierError(f"Не смог разобрать ответ ИИ: {raw!r}") from exc
    return set(int(x) for x in data.get("important_ids", []))
