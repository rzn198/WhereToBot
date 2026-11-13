"""Чистка таблицы event_reviews (пересмотры событий) по дате из config.yaml.

    event_reviews:
      cleanup_before: "2026-12-01"   # удалить пересмотры, созданные ДО этой даты; пусто / нет ключа = чистка выключена
      only_resolved: true            # true — только закрытые (status = 'resolved' или 'applied'); false — вместе с открытыми

Запускается из сервиса дедупликатора после остальных его задач (прогон дедупликации и перевод) не чаще раза в сутки
на одно значение настроек. Дата в будущем и непонятное значение игнорируются с предупреждением в логе — чтобы случайная
опечатка не стёрла свежие пересмотры. Удаление открытого пересмотра снова делает кнопку «Пересмотр» на карточке активной.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from loguru import logger

from db.tables import Tables


@dataclass(frozen=True)
class ReviewCleanupSettings:
    before: date | None = None
    only_resolved: bool = True


def review_cleanup_settings(cfg: dict | None, today: date | None = None) -> ReviewCleanupSettings:
    """Настройки из блока event_reviews. Пусто, мусор или дата в будущем → before=None (чистка не идёт)."""
    c = cfg or {}
    only_resolved = c.get('only_resolved', True)
    only_resolved = True if only_resolved is None else bool(only_resolved)
    raw = c.get('cleanup_before')
    if raw in (None, ''):
        return ReviewCleanupSettings(None, only_resolved)
    if isinstance(raw, datetime):
        day = raw.date()
    elif isinstance(raw, date):          # YAML сам превращает 2026-12-01 без кавычек в date
        day = raw
    else:
        try:
            day = date.fromisoformat(str(raw).strip())
        except ValueError:
            logger.warning(f"REVIEWS_CLEANUP_BAD_DATE | event_reviews.cleanup_before={raw!r} — ожидается ГГГГ-ММ-ДД, чистка пропущена")
            return ReviewCleanupSettings(None, only_resolved)
    if day > (today or date.today()):
        logger.warning(f"REVIEWS_CLEANUP_FUTURE_DATE | event_reviews.cleanup_before={day} позже сегодняшнего дня — чистка пропущена")
        return ReviewCleanupSettings(None, only_resolved)
    return ReviewCleanupSettings(day, only_resolved)


def _count(status: str) -> int:
    try:
        return int(status.split()[-1])
    except (ValueError, IndexError):
        return 0


async def purge_reviews(pool, st: ReviewCleanupSettings) -> int:
    """Удаляет пересмотры, созданные до st.before. Возвращает число удалённых."""
    if st.before is None:
        return 0
    sql = f"DELETE FROM {Tables.EVENT_REVIEWS} WHERE created_at < $1::date"
    if st.only_resolved:
        sql += " AND status IN ('resolved', 'applied')"
    return _count(await pool.execute(sql, st.before))


class ReviewCleanupJob:
    """Не чаще раза в сутки на одно значение настроек; сбой БД не роняет сервис."""

    def __init__(self, pool, cfg_getter):
        self._pool = pool
        self._cfg_getter = cfg_getter           # callable → dict блока event_reviews (перечитывается на каждый вызов)
        self._done_key: tuple | None = None

    async def run_if_due(self, today: date | None = None) -> int | None:
        """None — не запускалась (выключена, уже была сегодня или ошибка); число — сколько удалено."""
        today = today or date.today()
        st = review_cleanup_settings(self._cfg_getter(), today)
        if st.before is None:
            return None
        key = (today, st.before, st.only_resolved)
        if key == self._done_key:
            return None
        try:
            deleted = await purge_reviews(self._pool, st)
        except Exception as e:
            logger.error(f"REVIEWS_CLEANUP_FAILED | {type(e).__name__}: {e}")
            return None
        self._done_key = key
        logger.info(f"REVIEWS_CLEANUP | before={st.before} | only_resolved={st.only_resolved} | deleted={deleted}")
        return deleted
