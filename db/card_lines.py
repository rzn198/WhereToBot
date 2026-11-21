"""Таблица card_lines (миграция 019): ИИ-строки списка категорий. Запись и очистка; чтение для бота идёт JOIN-ом в window_rows_sql()."""
from __future__ import annotations

import asyncpg
from loguru import logger

from db.tables import Tables


async def save_line(pool: asyncpg.Pool, kind: str, ref_id: int, short_title: str, attr: str | None, profile: str,
                    src_hash: str, model: str | None, prompt_version: str, *, hook=None, hook_status: str | None = None,
                    hook_url: str | None = None, text_time=None) -> None:
    """hook — принятый card_lines.hooks.Hook или None; hook_status: ok | none | invalid | not_found | retry | None (хуки не просили)."""
    await pool.execute(
        f"INSERT INTO {Tables.CARD_LINES} (kind, ref_id, short_title, attr, profile, src_hash, model, prompt_version, "
        f"hook, hook_formula, hook_source, hook_evidence, hook_url, hook_status, text_time) "
        f"VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15) "
        f"ON CONFLICT (kind, ref_id) DO UPDATE SET short_title = EXCLUDED.short_title, attr = EXCLUDED.attr, "
        f"profile = EXCLUDED.profile, src_hash = EXCLUDED.src_hash, model = EXCLUDED.model, "
        f"prompt_version = EXCLUDED.prompt_version, hook = EXCLUDED.hook, hook_formula = EXCLUDED.hook_formula, "
        f"hook_source = EXCLUDED.hook_source, hook_evidence = EXCLUDED.hook_evidence, hook_url = EXCLUDED.hook_url, "
        f"hook_status = EXCLUDED.hook_status, text_time = EXCLUDED.text_time, updated_at = now()",
        kind, ref_id, short_title, attr, profile, src_hash, model, prompt_version,
        hook.text if hook else None, hook.formula if hook else None, hook.source if hook else None,
        hook.evidence if hook else None, hook_url or None, hook_status, text_time)


async def copy_by_hash(pool: asyncpg.Pool, cands, prompt_version: str) -> set[tuple[str, int]]:
    """Записи с тем же хэшем входных данных (повторы одного события на разные даты) берут готовую строку без запроса к модели.
    Не копируются строки, ожидающие повтора (not_found / retry). Возвращает ключи (kind, id) скопированных записей."""
    by_hash: dict[str, list] = {}
    for c in cands:
        by_hash.setdefault(c.src_hash, []).append(c)
    if not by_hash:
        return set()
    rows = await pool.fetch(
        f"SELECT DISTINCT ON (src_hash) * FROM {Tables.CARD_LINES} WHERE src_hash = ANY($1::text[]) AND prompt_version = $2 "
        f"AND coalesce(hook_status, '') NOT IN ('not_found', 'retry') ORDER BY src_hash, updated_at DESC",
        list(by_hash), prompt_version)
    done: set[tuple[str, int]] = set()
    for r in rows:
        for c in by_hash.get(r['src_hash'], []):
            await pool.execute(
                f"INSERT INTO {Tables.CARD_LINES} (kind, ref_id, short_title, attr, profile, src_hash, model, prompt_version, "
                f"hook, hook_formula, hook_source, hook_evidence, hook_url, hook_status, text_time) "
                f"VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15) "
                f"ON CONFLICT (kind, ref_id) DO UPDATE SET short_title = EXCLUDED.short_title, attr = EXCLUDED.attr, "
                f"profile = EXCLUDED.profile, src_hash = EXCLUDED.src_hash, model = EXCLUDED.model, "
                f"prompt_version = EXCLUDED.prompt_version, hook = EXCLUDED.hook, hook_formula = EXCLUDED.hook_formula, "
                f"hook_source = EXCLUDED.hook_source, hook_evidence = EXCLUDED.hook_evidence, hook_url = EXCLUDED.hook_url, "
                f"hook_status = EXCLUDED.hook_status, text_time = EXCLUDED.text_time, updated_at = now()",
                c.kind, c.id, r['short_title'], r['attr'], r['profile'], r['src_hash'], r['model'], r['prompt_version'],
                r['hook'], r['hook_formula'], r['hook_source'], r['hook_evidence'], r['hook_url'], r['hook_status'], r['text_time'])
            done.add(c.key)
    return done


async def purge_orphans(pool: asyncpg.Pool) -> int:
    """Удаляет строки записей, которых уже нет в events/messages (удалены очисткой). Вызывается из ежедневной очистки."""
    try:
        status = await pool.execute(
            f"DELETE FROM {Tables.CARD_LINES} c WHERE "
            f"(c.kind = 'e' AND NOT EXISTS (SELECT 1 FROM {Tables.EVENTS} e WHERE e.id = c.ref_id)) OR "
            f"(c.kind = 'm' AND NOT EXISTS (SELECT 1 FROM {Tables.MESSAGES} m WHERE m.surrogate_id = c.ref_id))")
        n = int(status.split()[-1])
        await pool.execute(f"DELETE FROM {Tables.CARD_ENRICHMENT} WHERE expires_at < now() - interval '7 days'")
        return n
    except Exception as e:
        logger.warning(f"CARD_LINES_PURGE_FAILED | {type(e).__name__}: {e}")
        return 0


async def get_enrichment(pool: asyncpg.Pool, key: str):
    """Непросроченная запись кэша обогащения или None."""
    from card_lines.enrich import Lookup
    r = await pool.fetchrow(
        f"SELECT status, facts, url FROM {Tables.CARD_ENRICHMENT} WHERE key = $1 AND expires_at > now()", key)
    return Lookup(r['status'], r['facts'] or '', r['url'] or '') if r else None


async def put_enrichment(pool: asyncpg.Pool, key: str, source: str, query: str, lookup, ttl_days: int) -> None:
    await pool.execute(
        f"INSERT INTO {Tables.CARD_ENRICHMENT} (key, source, query, status, facts, url, expires_at) "
        f"VALUES ($1, $2, $3, $4, $5, $6, now() + make_interval(days => $7::int)) "
        f"ON CONFLICT (key) DO UPDATE SET source = EXCLUDED.source, query = EXCLUDED.query, status = EXCLUDED.status, "
        f"facts = EXCLUDED.facts, url = EXCLUDED.url, fetched_at = now(), expires_at = EXCLUDED.expires_at",
        key, source, query, lookup.status, lookup.facts or None, lookup.url or None, int(ttl_days))
