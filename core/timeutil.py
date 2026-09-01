"""
Форматирует время сообщения в таймзоне из .env (TIMEZONE, по умолчанию
Europe/Moscow — раз узнать реальный часовой пояс её телефона программно
нельзя, это разумный дефолт, который она может переопределить в .env).
"""
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

_tz_cache: ZoneInfo | None = None


def _tz() -> ZoneInfo:
    global _tz_cache
    if _tz_cache is None:
        _tz_cache = ZoneInfo(os.environ.get("TIMEZONE", "Europe/Moscow"))
    return _tz_cache


def format_time(dt_utc_naive: datetime) -> str:
    """dt_utc_naive — как хранится в БД: naive datetime, но по факту в UTC."""
    aware_utc = dt_utc_naive.replace(tzinfo=timezone.utc)
    local = aware_utc.astimezone(_tz())
    return local.strftime("%H:%M %d.%m")


def local_now() -> datetime:
    return datetime.now(_tz())


def today_start_utc_naive() -> datetime:
    """Начало сегодняшнего дня в её таймзоне, переведённое в наивный UTC —
    тот же формат, в котором хранится Message.sent_at, чтобы можно было
    прямо сравнивать в SQL-запросе (используется в /stats)."""
    start_local = local_now().replace(hour=0, minute=0, second=0, microsecond=0)
    return start_local.astimezone(timezone.utc).replace(tzinfo=None)
