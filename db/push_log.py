"""Журнал ежедневных пушей (таблица push_log): что уже отправлено, кто заблокировал бота, чистка."""
from datetime import date

from db.tables import Tables

KEEP_DAYS = 30            # сколько хранить журнал
BLOCKED_RETRY_DAYS = 7    # заблокировавшему боту пользователю пробуем снова не чаще, чем раз в эту неделю


async def sent_users(pool, day: date) -> set[int]:
    """Кому за этот день уже отправили (повторно не шлём)."""
    rows = await pool.fetch(
        f"SELECT user_id FROM {Tables.PUSH_LOG} WHERE push_date = $1 AND status = 'sent'", day)
    return {r['user_id'] for r in rows}


async def recently_blocked(pool, day: date) -> set[int]:
    """Кто заблокировал бота в последние BLOCKED_RETRY_DAYS дней (до сегодняшнего дня) — пропускаем."""
    rows = await pool.fetch(
        f"SELECT DISTINCT user_id FROM {Tables.PUSH_LOG} "
        f"WHERE status = 'blocked' AND push_date < $1 AND push_date >= $1 - $2::int", day, BLOCKED_RETRY_DAYS)
    return {r['user_id'] for r in rows}


async def record(pool, day: date, user_id: int, status: str, items: int, parts: int = 1, error: str | None = None) -> None:
    await pool.execute(
        f"""INSERT INTO {Tables.PUSH_LOG} (push_date, user_id, status, items, parts, error)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (push_date, user_id) DO UPDATE
            SET status = EXCLUDED.status, items = EXCLUDED.items, parts = EXCLUDED.parts,
                error = EXCLUDED.error, sent_at = now()""",
        day, user_id, status, items, parts, (error or None) and error[:300])


async def purge_old(pool, days: int = KEEP_DAYS) -> int:
    status = await pool.execute(
        f"DELETE FROM {Tables.PUSH_LOG} WHERE push_date < CURRENT_DATE - make_interval(days => $1)", days)
    try:
        return int(status.split()[-1])
    except (ValueError, IndexError):
        return 0
