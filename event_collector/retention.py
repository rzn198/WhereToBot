from __future__ import annotations

from datetime import date, datetime, timedelta

from shared.config import config

DEFAULT_PAST_DAYS = 7
DEFAULT_FUTURE_DAYS = 365


def get_window() -> tuple[int, int]:
    cfg = config.get('cleanup', {}) or {}
    past = int(cfg.get('events_cleanup_days', DEFAULT_PAST_DAYS))
    future = int(cfg.get('events_max_future_days', DEFAULT_FUTURE_DAYS))
    return past, future


def _to_date(value: date | datetime) -> date:
    return value.date() if isinstance(value, datetime) else value


def in_window(
    starts_at: date | datetime | None,
    today: date | None = None,
    past_days: int | None = None,
    future_days: int | None = None,
) -> bool:
    if starts_at is None:
        return True
    if past_days is None or future_days is None:
        cfg_past, cfg_future = get_window()
        past_days = cfg_past if past_days is None else past_days
        future_days = cfg_future if future_days is None else future_days
    today = today or date.today()
    d = _to_date(starts_at)
    return today - timedelta(days=past_days) <= d <= today + timedelta(days=future_days)


def too_far(
    starts_at: date | datetime | None,
    today: date | None = None,
    future_days: int | None = None,
) -> bool:
    if starts_at is None:
        return False
    if future_days is None:
        future_days = get_window()[1]
    today = today or date.today()
    return _to_date(starts_at) > today + timedelta(days=future_days)
