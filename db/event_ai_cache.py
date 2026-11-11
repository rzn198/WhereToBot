"""Кэш AI-категорий событий (таблица event_ai_categories, миграция 011). Все функции best-effort:
при ошибке БД (например, миграция ещё не применена) работаем без кэша, сбор событий не падает."""
from __future__ import annotations

import asyncpg
from loguru import logger

from db.tables import Tables


async def load_cached(pool: asyncpg.Pool, source: str, urls: list[str]) -> dict[str, tuple[str, list[str]]]:
    """{source_url: (text_hash, categories)}."""
    if pool is None or not urls:
        return {}
    try:
        rows = await pool.fetch(
            f"SELECT source_url, text_hash, categories FROM {Tables.EVENT_AI_CATEGORIES} "
            f"WHERE source = $1 AND source_url = ANY($2::text[])", source, list(urls))
        return {r['source_url']: (r['text_hash'], list(r['categories'] or [])) for r in rows}
    except Exception as e:
        logger.warning(f"EVENT_AI_CACHE_UNAVAILABLE | {type(e).__name__}: {e} — кэш не используется")
        return {}


async def save_cached(pool: asyncpg.Pool, source: str, url: str, text_hash: str,
                      categories: list[str], model: str | None) -> None:
    if pool is None:
        return
    try:
        await pool.execute(
            f"INSERT INTO {Tables.EVENT_AI_CATEGORIES} (source, source_url, text_hash, categories, model) "
            f"VALUES ($1, $2, $3, $4, $5) "
            f"ON CONFLICT (source, source_url) DO UPDATE SET text_hash = EXCLUDED.text_hash, "
            f"categories = EXCLUDED.categories, model = EXCLUDED.model, decided_at = now()",
            source, url, text_hash, list(categories), model)
    except Exception as e:
        logger.warning(f"EVENT_AI_CACHE_WRITE_FAILED | {type(e).__name__}: {e}")


async def purge_orphans(pool: asyncpg.Pool) -> int:
    """Удаляет записи кэша, для которых события уже нет в events (удалены очисткой). Вызывается из ежедневной очистки."""
    try:
        status = await pool.execute(
            f"DELETE FROM {Tables.EVENT_AI_CATEGORIES} c WHERE NOT EXISTS ("
            f"SELECT 1 FROM {Tables.EVENTS} e WHERE e.source = c.source AND e.source_url = c.source_url)")
        return int(status.split()[-1])
    except Exception as e:
        logger.warning(f"EVENT_AI_CACHE_PURGE_FAILED | {type(e).__name__}: {e}")
        return 0
