import asyncpg
from loguru import logger

from db.ai_seen import purge_old
from db.tables import Tables
from event_collector.retention import get_window
from shared.config import config


def _count(status: str) -> int:
    try:
        return int(status.split()[-1])
    except (ValueError, IndexError):
        return 0


COLLECTOR_RUNS_KEEP_DAYS = 14
COLLECTOR_RUNS_KEEP_LAST = 50


async def purge_dedup_history(pool: asyncpg.Pool, days: int) -> dict:
    """Служебные таблицы дедупликатора и журнал прогонов коллекторов не должны расти бесконечно.

    * dedup_decisions / dedup_dates — записи по датам старше days дней (движок смотрит только на даты от сегодня);
    * dedup_runs — старше days дней, но всегда остаются последний прогон и последний прогон ai
      (по нему had_ai_run понимает, что ai уже включали); идущий прогон не трогаем;
    * collector_runs — завершённые старше 14 дней, кроме последних 50 строк (по ним decide() видит, когда коллекторы
      закончили); незавершённые не трогаем.
    Ссылки решений и дат на удалённые прогоны — ON DELETE SET NULL. Сбой одного шага не мешает остальным."""
    steps = {
        'decisions': (f"DELETE FROM {Tables.DEDUP_DECISIONS} WHERE day < CURRENT_DATE - make_interval(days => $1)", days),
        'dates': (f"DELETE FROM {Tables.DEDUP_DATES} WHERE day < CURRENT_DATE - make_interval(days => $1)", days),
        'runs': (
            f"DELETE FROM {Tables.DEDUP_RUNS} WHERE started_at < now() - make_interval(days => $1) "
            f"AND status <> 'running' "
            f"AND id <> (SELECT max(id) FROM {Tables.DEDUP_RUNS}) "
            f"AND id <> COALESCE((SELECT max(id) FROM {Tables.DEDUP_RUNS} WHERE method = 'ai'), -1)", days),
        'collector_runs': (
            f"DELETE FROM {Tables.COLLECTOR_RUNS} WHERE finished_at IS NOT NULL "
            f"AND finished_at < now() - make_interval(days => $1) "
            f"AND id NOT IN (SELECT id FROM {Tables.COLLECTOR_RUNS} ORDER BY id DESC LIMIT {COLLECTOR_RUNS_KEEP_LAST})",
            COLLECTOR_RUNS_KEEP_DAYS),
    }
    out = {}
    for name, (sql, arg) in steps.items():
        try:
            out[name] = _count(await pool.execute(sql, arg))
        except Exception as e:
            out[name] = 0
            logger.warning(f"DEDUP_HISTORY_PURGE_FAILED | {name} | {type(e).__name__}: {e}")
    return out


async def run_cleanup(pool: asyncpg.Pool) -> dict:
    cfg = config.get('cleanup', {})
    event_cleanup_days = int(cfg.get('event_cleanup_days', 30))
    no_date_cleanup_days = int(cfg.get('no_date_cleanup_days', 7))
    events_past_days, events_future_days = get_window()

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(f"""
                CREATE TEMP TABLE _cleanup_ids ON COMMIT DROP AS
                WITH RECURSIVE expired AS (
                    SELECT surrogate_id, duplicate_of
                    FROM {Tables.MESSAGES}
                    WHERE (date IS NOT NULL
                           AND date < CURRENT_DATE - make_interval(days => $1))
                       OR (date IS NULL
                           AND created_at < NOW() - make_interval(days => $2))
                ),
                held AS (
                    -- оригинал, у которого есть копия вне списка удаляемых
                    SELECT e.surrogate_id
                    FROM expired e
                    WHERE EXISTS (
                        SELECT 1 FROM {Tables.MESSAGES} c
                        WHERE c.duplicate_of = e.surrogate_id
                          AND c.surrogate_id NOT IN (SELECT surrogate_id FROM expired)
                    )
                    UNION
                    -- цепочка: оригинал удерживаемой копии тоже удерживается
                    SELECT e.surrogate_id
                    FROM expired e
                    JOIN {Tables.MESSAGES} m ON m.duplicate_of = e.surrogate_id
                    JOIN held h ON h.surrogate_id = m.surrogate_id
                )
                SELECT e.surrogate_id,
                       EXISTS (SELECT 1 FROM held h WHERE h.surrogate_id = e.surrogate_id) AS is_held
                FROM expired e
            """, event_cleanup_days, no_date_cleanup_days)

            held_count = await conn.fetchval(
                "SELECT count(*) FROM _cleanup_ids WHERE is_held"
            )

            result_alerts = await conn.execute(f"""
                UPDATE {Tables.SCHEDULED_ALERTS}
                SET msg_surrogate_id = NULL
                WHERE msg_surrogate_id IN (
                    SELECT surrogate_id FROM _cleanup_ids WHERE NOT is_held
                )
            """)

            rows = await conn.fetch(f"""
                DELETE FROM {Tables.MESSAGES}
                WHERE surrogate_id IN (
                    SELECT surrogate_id FROM _cleanup_ids WHERE NOT is_held
                )
                RETURNING date IS NULL AS no_date
            """)

            events_deleted = _count(await conn.execute(f"""
                DELETE FROM {Tables.EVENTS}
                WHERE starts_at < CURRENT_DATE - make_interval(days => $1)
                   OR starts_at > CURRENT_DATE + make_interval(days => $2)
            """, events_past_days, events_future_days))

    seen_purged = await purge_old(pool, int((config.get('parsing') or {}).get('seen_retention_days', 60)))
    history = await purge_dedup_history(pool, int((config.get('deduplication') or {}).get('history_days', 60)))

    no_date_count = sum(1 for r in rows if r['no_date'])
    dated_count = len(rows) - no_date_count
    alerts_detached = _count(result_alerts)

    logger.info(
        f"CLEANUP | dated={dated_count} | no_date={no_date_count} | "
        f"held_by_dup_refs={held_count} | alerts_detached={alerts_detached} | "
        f"events={events_deleted} | ai_seen={seen_purged} | dedup_history={history}"
    )

    return {
        'dated': dated_count,
        'no_date': no_date_count,
        'held': held_count,
        'alerts_detached': alerts_detached,
        'events': events_deleted,
        'ai_seen': seen_purged,
        'dedup_history': history,
    }
