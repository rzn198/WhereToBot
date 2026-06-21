import asyncpg
from loguru import logger

from db.tables import Tables
from shared.config import config


async def run_cleanup(pool: asyncpg.Pool) -> dict:
    cfg = config.get('cleanup', {})
    event_cleanup_days = int(cfg.get('event_cleanup_days', 30))
    no_date_cleanup_days = int(cfg.get('no_date_cleanup_days', 7))

    result_dated = await pool.execute(f"""
        DELETE FROM {Tables.MESSAGES}
        WHERE date IS NOT NULL
        AND date < CURRENT_DATE - INTERVAL '{event_cleanup_days} days'
    """)

    result_no_date = await pool.execute(f"""
        DELETE FROM {Tables.MESSAGES}
        WHERE date IS NULL
        AND created_at < NOW() - INTERVAL '{no_date_cleanup_days} days'
    """)

    dated_count = int(result_dated.split()[-1])
    no_date_count = int(result_no_date.split()[-1])

    logger.info(
        f"CLEANUP | dated={dated_count} | no_date={no_date_count}"
    )

    return {
        'dated': dated_count,
        'no_date': no_date_count,
    }