"""Запросы на ручной запуск дедупликатора (таблица dedup_requests): бот создаёт, сервис deduplicator забирает."""
from __future__ import annotations

import asyncpg

from db.tables import Tables

EXPIRE_HOURS = 6        # запрос, который никто не взял за 6 часов, отменяется: позднего неожиданного запуска не хотим
KEEP_DAYS = 30


async def create(pool, user_id: int | None, force: bool) -> int | None:
    """Новый запрос. None — уже есть открытый (pending/running)."""
    await expire_old(pool)
    await pool.execute(f"DELETE FROM {Tables.DEDUP_REQUESTS} WHERE status NOT IN ('pending', 'running') "
                       f"AND requested_at < now() - make_interval(days => $1)", KEEP_DAYS)
    try:
        return await pool.fetchval(
            f"INSERT INTO {Tables.DEDUP_REQUESTS} (requested_by, force) VALUES ($1, $2) RETURNING id", user_id, force)
    except asyncpg.UniqueViolationError:
        return None


async def expire_old(pool) -> int:
    res = await pool.execute(
        f"UPDATE {Tables.DEDUP_REQUESTS} SET status = 'cancelled', finished_at = now(), "
        f"error = 'не взят в работу за ' || $1::text || ' ч' "
        f"WHERE status = 'pending' AND requested_at < now() - make_interval(hours => $1)", EXPIRE_HOURS)
    return int(res.split()[-1])


async def open_request(pool):
    """Открытый запрос (pending/running) или None."""
    return await pool.fetchrow(
        f"SELECT * FROM {Tables.DEDUP_REQUESTS} WHERE status IN ('pending', 'running') ORDER BY id LIMIT 1")


async def last_finished(pool):
    return await pool.fetchrow(
        f"SELECT * FROM {Tables.DEDUP_REQUESTS} WHERE status IN ('done', 'failed') ORDER BY id DESC LIMIT 1")


async def has_pending(pool) -> bool:
    await expire_old(pool)
    return bool(await pool.fetchval(f"SELECT EXISTS (SELECT 1 FROM {Tables.DEDUP_REQUESTS} WHERE status = 'pending')"))


async def cancel_pending(pool) -> bool:
    """Отмена, пока запрос не взят в работу. Запущенный прогон не прерываем."""
    res = await pool.execute(
        f"UPDATE {Tables.DEDUP_REQUESTS} SET status = 'cancelled', finished_at = now(), error = 'отменён админом' "
        f"WHERE status = 'pending'")
    return int(res.split()[-1]) > 0


async def claim(pool):
    """Сервис берёт запрос в работу: pending → running. None — брать нечего."""
    return await pool.fetchrow(
        f"UPDATE {Tables.DEDUP_REQUESTS} SET status = 'running', started_at = now() "
        f"WHERE id = (SELECT id FROM {Tables.DEDUP_REQUESTS} WHERE status = 'pending' ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED) "
        f"RETURNING *")


async def release(pool, req_id: int) -> None:
    """Блокировку не получили (идёт другой прогон): запрос возвращается в очередь."""
    await pool.execute(f"UPDATE {Tables.DEDUP_REQUESTS} SET status = 'pending', started_at = NULL "
                       f"WHERE id = $1 AND status = 'running'", req_id)


async def finish(pool, req_id: int, ok: bool, run_id: int | None, error: str | None = None) -> None:
    await pool.execute(
        f"UPDATE {Tables.DEDUP_REQUESTS} SET status = $2, finished_at = now(), run_id = $3, error = $4 WHERE id = $1",
        req_id, 'done' if ok else 'failed', run_id, (error or None) and error[:300])


async def close_interrupted(pool) -> int:
    """При старте сервиса: запрос 'running' остался после падения → failed."""
    res = await pool.execute(
        f"UPDATE {Tables.DEDUP_REQUESTS} SET status = 'failed', finished_at = now(), error = 'прерван перезапуском сервиса' "
        f"WHERE status = 'running'")
    return int(res.split()[-1])


async def status_info(pool) -> dict:
    """Данные для экрана админки: последний прогон дедупа, работающие коллекторы, открытый запрос."""
    last = await pool.fetchrow(
        f"SELECT id, started_at, finished_at, method, status, dates_processed, dates_total, hidden, unhidden, error "
        f"FROM {Tables.DEDUP_RUNS} ORDER BY id DESC LIMIT 1")
    active = await pool.fetchval(f"SELECT count(*) FROM {Tables.COLLECTOR_RUNS} WHERE finished_at IS NULL "
                                 f"AND heartbeat_at > now() - interval '15 minutes'")
    return dict(last=last, collectors=int(active or 0), open=await open_request(pool), last_request=await last_finished(pool))
