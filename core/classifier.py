"""
Классификатор поверх Gemini API: classify_single (одно escalated-сообщение)
и classify_batch (пачка hourly-сообщений, ответ — только id важных).

Несколько ключей через запятую в GEMINI_API_KEY — крутятся по кругу,
упавший пропускается. add_extra_keys() добавляет резервные ключи на лету
(команда /api), сохраняя их в extra_gemini_keys.txt на случай перезапуска.
"""
import asyncio
import datetime as dt
import itertools
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

MODEL_NAME = "gemini-3.6-flash"

# Резервные модели — пробуются по очереди, ТОЛЬКО когда основная модель
# исчерпала дневную квоту на всех ключах. У другой модели в том же Google
# Cloud проекте своя ОТДЕЛЬНАЯ квота (лимит "PerProjectPerModel"), поэтому
# это реально добавляет бюджет, а не просто перебирает те же ключи.
# Задаётся через .env: GEMINI_FALLBACK_MODELS=gemini-2.5-flash,gemini-2.5-flash-lite
_fallback_models_raw = os.environ.get("GEMINI_FALLBACK_MODELS", "")
MODELS: list[str] = [MODEL_NAME] + [m.strip() for m in _fallback_models_raw.split(",") if m.strip()]

_default_extra_keys_path = Path(__file__).resolve().parent.parent / "extra_gemini_keys.txt"
EXTRA_KEYS_PATH = Path(os.environ.get("EXTRA_GEMINI_KEYS_PATH", str(_default_extra_keys_path)))

_clients: list[genai.Client] = []
_keys: list[str] = []  # для проверки дублей при добавлении новых
_key_cycle = None  # инициализируется лениво/пересоздаётся при изменении _clients

# (индекс ключа, имя модели) -> дата (UTC), в которую эта пара упёрлась в
# ДНЕВНУЮ квоту — пока дата совпадает с сегодняшней, пробовать бессмысленно
# (сбрасывается само по себе на следующий день, ключ по дате не совпадёт).
_exhausted_today: dict[tuple[int, str], "dt.date"] = {}

# (индекс ключа, имя модели) -> time.monotonic(), до которого эта пара
# "отдыхает" после ПОМИНУТНОГО (RPM) лимита — в отличие от дневной квоты,
# это не навсегда, а секунд на 20-30, дальше снова можно пробовать.
_cooldown_until: dict[tuple[int, str], float] = {}

RPM_COOLDOWN_SECONDS = 25
ALL_THROTTLED_RETRY_SECONDS = 12


def _is_daily_quota_error(exc: Exception) -> bool:
    """ДНЕВНАЯ квота проекта кончилась — ждать бессмысленно вообще, нужна
    другая модель или завтрашний день."""
    msg = str(exc)
    return "RESOURCE_EXHAUSTED" in msg and "PerDay" in msg


def _is_rate_limit_error(exc: Exception) -> bool:
    """Временный "Too Many Requests" (обычно поминутный RPM-лимит) — не
    настоящее исчерпание, через секунды снова заработает. Отличаем от
    дневной квоты (та уже поймана выше) и от прочих ошибок (503 и т.п.,
    для них смысла в персональном "остывании" ключа нет)."""
    msg = str(exc)
    return "RESOURCE_EXHAUSTED" in msg or "429" in msg or "Too Many Requests" in msg


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


async def _call_model(contents: Union[str, list], _retried_after_throttle: bool = False) -> str:
    """Round-robin по ключам для основной модели; если ВСЕ ключи упёрлись в
    дневную квоту на ней — переходим к следующей модели из MODELS (у неё
    своя отдельная квота на тех же проектах) и пробуем ключи заново.

    Дневной лимит (PerDay) и поминутный (RPM/"Too Many Requests") — разные
    вещи: дневной откладывает пару ключ+модель до завтра, поминутный — на
    ~RPM_COOLDOWN_SECONDS. Если СРАЗУ ВСЕ попытки в этом вызове упёрлись
    только в поминутный лимит (не в дневной и не в другую ошибку) — это
    временный затор, а не "ключи кончились": ждём немного и пробуем весь
    цикл ещё один раз, прежде чем сдаваться по-настоящему.

    contents — строка или список (текст вперемешку с
    types.Part.from_bytes(...) для вложений/аудио)."""
    _ensure_clients()
    n = len(_clients)
    start = next(_key_cycle)
    today = dt.datetime.utcnow().date()
    now = time.monotonic()

    last_exc: Optional[Exception] = None
    saw_failure = False
    all_failures_were_throttle = True

    for model in MODELS:
        model_had_live_key = False
        for offset in range(n):
            idx = (start + offset) % n
            if _exhausted_today.get((idx, model)) == today:
                continue
            if _cooldown_until.get((idx, model), 0.0) > now:
                continue
            model_had_live_key = True
            try:
                # generate_content — синхронный, блокирующий вызов (SDK не
                # asyncio-friendly из коробки). Без to_thread он бы стопорил
                # ВЕСЬ event loop на время запроса — все боты, слушатели,
                # даже обработку Ctrl+C, — не только эту конкретную задачу.
                response = await asyncio.to_thread(
                    _clients[idx].models.generate_content, model=model, contents=contents
                )
                return response.text
            except Exception as exc:
                saw_failure = True
                if _is_daily_quota_error(exc):
                    _exhausted_today[(idx, model)] = today
                    all_failures_were_throttle = False
                    logger.warning(
                        "Gemini-ключ #%d (%s) исчерпал дневную квоту, откладываю до завтра", idx + 1, model
                    )
                elif _is_rate_limit_error(exc):
                    _cooldown_until[(idx, model)] = now + RPM_COOLDOWN_SECONDS
                    logger.warning(
                        "Gemini-ключ #%d (%s) словил Too Many Requests, отдыхает %d с.",
                        idx + 1, model, RPM_COOLDOWN_SECONDS,
                    )
                else:
                    all_failures_were_throttle = False
                    logger.warning("Gemini-ключ #%d (%s) не сработал (%s), пробую следующий", idx + 1, model, exc)
                last_exc = exc
                continue
        if not model_had_live_key:
            logger.warning("У модели %s сейчас не осталось живых ключей (квота/остывают), пробую следующую модель", model)
        elif model is not MODELS[-1]:
            logger.warning("Все ключи не сработали на модели %s, пробую следующую модель", model)

    if saw_failure and all_failures_were_throttle and not _retried_after_throttle:
        logger.warning(
            "Все ключи временно упёрлись в Too Many Requests — жду %d с. и пробую ещё раз, прежде чем сдаваться",
            ALL_THROTTLED_RETRY_SECONDS,
        )
        await asyncio.sleep(ALL_THROTTLED_RETRY_SECONDS)
        return await _call_model(contents, _retried_after_throttle=True)

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


async def extract_school_profile(base_profile_text: str, school_name: str) -> str:
    """Из общего /profile вычленяет скрытый профиль под конкретную школу:
    общая информация (имя, профессия и т.п.) + только то, что относится к
    этой школе, без упоминаний других школ. Пересчитывается при создании
    школы и при каждом изменении /profile."""
    prompt = f"""Ниже — полное описание пользователя (может касаться нескольких школ/мест работы сразу).

{base_profile_text}

Составь версию этого описания ТОЛЬКО для школы «{school_name}»: оставь общую
информацию, которая касается пользователя в целом (имя, профессия, предмет
и т.п.), и ту часть, что относится именно к «{school_name}» (классы,
обязанности, роли). Полностью убери всё, что относится к другим
школам/местам. Верни только итоговый текст, без пояснений, кавычек и
markdown-разметки."""
    raw = await _call_model(prompt)
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

Важно: если название чата само по себе указывает на тему (например, чат
называется «Химия», «8-9 классы» или «Летово химия 26-27»), а сообщение
просто касается этой же темы — само по себе это НЕ основание для
важности. Раз весь чат и так посвящён этой теме, упоминание её там
неинформативно (это как пометить важным любое сообщение в чате "Погода"
только за то, что оно про погоду). Оценивай, требует ли сообщение
реального личного участия, решения или действия получателя — а не
формальное совпадение с её профилем. Обычная бытовая переписка
(созвониться, зайти, спросить как дела, о чём-то узнать без конкретики)
сама по себе не важна, даже если она про её предмет или класс.

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

Важно: если название чата само по себе указывает на тему (например, чат
называется «Химия», «8-9 классы» или «Летово химия 26-27»), а сообщение
просто касается этой же темы — само по себе это НЕ основание для
важности. Раз весь чат и так посвящён этой теме, упоминание её там
неинформативно. Оценивай, требует ли сообщение реального личного
участия, решения или действия получателя — а не формальное совпадение с
её профилем. Обычная бытовая переписка (созвониться, зайти, спросить как
дела, о чём-то узнать без конкретики) сама по себе не важна, даже если
она про её предмет или класс.

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
