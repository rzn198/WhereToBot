"""Время и «сегодня» для пользовательской части бота: всегда по Белграду.

Контейнеры и Postgres живут в UTC, поэтому `date.today()` и `CURRENT_DATE` между 00:00 и 01:00–02:00 по Белграду
ещё показывали вчерашнюю дату. Пользовательские выборки (лента, календари, счётчики) используют эти помощники.
Сборщики, чистка и расписания остаются на UTC: отклонение в 1–2 часа им не вредит.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

BELGRADE_TZ = ZoneInfo('Europe/Belgrade')

# То же самое для SQL (вместо CURRENT_DATE).
BELGRADE_TODAY_SQL = "(now() AT TIME ZONE 'Europe/Belgrade')::date"


def belgrade_now() -> datetime:
    return datetime.now(BELGRADE_TZ)


def belgrade_today() -> date:
    """Сегодняшняя дата в Белграде. Тесты подменяют shared.tz.belgrade_now."""
    return belgrade_now().date()
