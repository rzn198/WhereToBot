import asyncpg
from loguru import logger

from db.tables import Tables
from shared.config import config


def _count(status: str) -> int:
    try:
        return int(status.split()[-1])
    except (ValueError, IndexError):
        return 0


async def run_cleanup(pool: asyncpg.Pool) -> dict:
    cfg = config.get('cleanup', {})
    event_cleanup_days = int(cfg.get('event_cleanup_days', 30))
    no_date_cleanup_days = int(cfg.get('no_date_cleanup_days', 7))

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

    no_date_count = sum(1 for r in rows if r['no_date'])
    dated_count = len(rows) - no_date_count
    alerts_detached = _count(result_alerts)

    logger.info(
        f"CLEANUP | dated={dated_count} | no_date={no_date_count} | "
        f"held_by_dup_refs={held_count} | alerts_detached={alerts_detached}"
    )

    return {
        'dated': dated_count,
        'no_date': no_date_count,
        'held': held_count,
        'alerts_detached': alerts_detached,
    }
