"""Запуск AI киноафиши из cinema_collector после каждого прогона скраперов (не блокирует цикл коллектора).

cinema_ai.method: off — ничего | shadow — только лог «сколько работы» | on — названия, жанры, переводы пишутся в БД.
Два прохода одновременно не идут: пока предыдущий работает, новый запуск пропускается (работа подхватится следующим).
"""
from __future__ import annotations

import asyncio

from loguru import logger

from cinema_ai import store
from cinema_ai.service import plan, run_pass
from cinema_ai.settings import cinema_ai_settings
from shared.ai import client_from_config
from shared.config import config

_running = False
_tasks: set[asyncio.Task] = set()


async def run_cinema_ai(pool, cfg: dict | None = None) -> None:
    global _running
    cfg = cfg if cfg is not None else (config.get('cinema_ai') or {})
    st = cinema_ai_settings(cfg)
    if not st.enabled:
        return
    if _running:
        logger.info("CINEMA_AI_SKIP | предыдущий проход ещё идёт")
        return
    _running = True
    client = None
    try:
        movies = await store.load_active(pool)
        if st.method == 'shadow':
            a, b = plan(movies, st)
            chars = sum(m['description_src_len'] for m in b)
            logger.info(f"CINEMA_AI_SHADOW | фильмов={len(movies)} | названия={len(a)} | переводы={len(b)} "
                        f"(~{chars} символов) | в БД ничего не пишется")
            return
        client = client_from_config('cinema-ai', cfg)
        await run_pass(pool, client, st, apply=True)
    except Exception as e:                                      # AI киноафиши не должен ломать сбор афиши
        logger.error(f"CINEMA_AI_ERROR | {type(e).__name__}: {e}")
    finally:
        _running = False
        if client is not None:
            await client.aclose()


def trigger(pool) -> None:
    """Фоновый запуск; ссылка на задачу хранится, чтобы её не собрал сборщик мусора."""
    if not cinema_ai_settings(config.get('cinema_ai') or {}).enabled:
        return
    task = asyncio.create_task(run_cinema_ai(pool))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
