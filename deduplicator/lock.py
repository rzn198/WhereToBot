"""Один прогон дедупликации за раз: advisory-блокировка Postgres.

Блокировку берут сервисный tick и ручной запуск (run_once): они не пересекутся, как и два контейнера
deduplicator. Блокировка сессионная — держится, пока соединение занято, и сама снимается при его потере.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

LOCK_KEY = 7451700101


@asynccontextmanager
async def try_lock(pool):
    """async with try_lock(pool) as got: got=True — блокировка наша, False — прогон уже идёт."""
    conn = await pool.acquire()
    got = False
    try:
        got = bool(await conn.fetchval("SELECT pg_try_advisory_lock($1)", LOCK_KEY))
        yield got
    finally:
        try:
            if got:
                await conn.execute("SELECT pg_advisory_unlock($1)", LOCK_KEY)
        finally:
            await pool.release(conn)
