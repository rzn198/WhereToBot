"""Таблица card_lines (миграция 019): ИИ-строки списка категорий. Запись и очистка; чтение для бота идёт JOIN-ом в window_rows_sql()."""
from __future__ import annotations

import asyncpg
from loguru import logger

from db.tables import Tables


async def save_line(pool: asyncpg.Pool, kind: str, ref_id: int, short_title: str, attr: str | None, profile: str,
                    src_hash: str, model: str | None, prompt_version: str) -> None:
    await pool.execute(
        f"INSERT INTO {Tables.CARD_LINES} (kind, ref_id, short_title, attr, profile, src_hash, model, prompt_version) "
        f"VALUES ($1, $2, $3, $4, $5, $6, $7, $8) "
        f"ON CONFLICT (kind, ref_id) DO UPDATE SET short_title = EXCLUDED.short_title, attr = EXCLUDED.attr, "
        f"profile = EXCLUDED.profile, src_hash = EXCLUDED.src_hash, model = EXCLUDED.model, "
        f"prompt_version = EXCLUDED.prompt_version, updated_at = now()",
        kind, ref_id, short_title, attr, profile, src_hash, model, prompt_version)


async def purge_orphans(pool: asyncpg.Pool) -> int:
    """Удаляет строки записей, которых уже нет в events/messages (удалены очисткой). Вызывается из ежедневной очистки."""
    try:
        status = await pool.execute(
            f"DELETE FROM {Tables.CARD_LINES} c WHERE "
            f"(c.kind = 'e' AND NOT EXISTS (SELECT 1 FROM {Tables.EVENTS} e WHERE e.id = c.ref_id)) OR "
            f"(c.kind = 'm' AND NOT EXISTS (SELECT 1 FROM {Tables.MESSAGES} m WHERE m.surrogate_id = c.ref_id))")
        return int(status.split()[-1])
    except Exception as e:
        logger.warning(f"CARD_LINES_PURGE_FAILED | {type(e).__name__}: {e}")
        return 0
