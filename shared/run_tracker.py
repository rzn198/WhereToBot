"""Журнал прогонов коллекторов (таблица collector_runs): по нему deduplicator понимает, что коллекторы закончили.

track_run — асинхронный контекст на время одного прогона: запись 'running' с пульсом (heartbeat), в конце
'ok' или 'error'. Это вспомогательный журнал: любой сбой БД только пишется в лог и никогда не ломает сбор.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from loguru import logger

from db.tables import Tables

HEARTBEAT_SECONDS = 60


async def _heartbeat(pool, run_id: int, seconds: float) -> None:
    while True:
        await asyncio.sleep(seconds)
        try:
            await pool.execute(
                f"UPDATE {Tables.COLLECTOR_RUNS} SET heartbeat_at = now() WHERE id = $1 AND finished_at IS NULL", run_id)
        except Exception as e:
            logger.warning(f"RUN_TRACKER_HEARTBEAT_ERROR | run_id={run_id} | {e}")


@asynccontextmanager
async def track_run(pool, service: str, heartbeat_seconds: float = HEARTBEAT_SECONDS):
    run_id = None
    try:
        run_id = await pool.fetchval(
            f"INSERT INTO {Tables.COLLECTOR_RUNS} (service) VALUES ($1) RETURNING id", service)
    except Exception as e:
        logger.warning(f"RUN_TRACKER_START_ERROR | service={service} | {type(e).__name__}: {e}")
    task = asyncio.ensure_future(_heartbeat(pool, run_id, heartbeat_seconds)) if run_id is not None else None
    status = 'ok'
    try:
        yield run_id
    except BaseException:
        status = 'error'
        raise
    finally:
        if task is not None:
            task.cancel()
        if run_id is not None:
            try:
                await asyncio.shield(pool.execute(
                    f"UPDATE {Tables.COLLECTOR_RUNS} SET finished_at = now(), heartbeat_at = now(), status = $2 "
                    f"WHERE id = $1", run_id, status))
            except BaseException as e:           # в том числе отмена задачи: запись закроет deduplicator как «зависшую»
                logger.warning(f"RUN_TRACKER_FINISH_ERROR | service={service} | run_id={run_id} | {type(e).__name__}: {e}")
