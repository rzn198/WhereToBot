"""Работа с БД: загрузка записей и решений, применение плана одной даты."""
from __future__ import annotations

from datetime import date

import asyncpg
from loguru import logger

from db.tables import Tables
from deduplicator.engine import DayPlan, Live
from deduplicator.records import KIND_EVENT, KIND_MESSAGE, Record

EVENT_DESC_CHARS = 400


def _event_text(row) -> str:
    parts = [row['title'] or '', row['venue'] or '', (row['description_ru'] or row['description'] or '')[:EVENT_DESC_CHARS]]
    return '. '.join(p.strip() for p in parts if p and p.strip())


async def load_channel_names(pool: asyncpg.Pool) -> dict[int, str]:
    rows = await pool.fetch(f"SELECT channel_id, username FROM {Tables.CHANNELS} WHERE username IS NOT NULL")
    return {r['channel_id']: r['username'] for r in rows}


async def load_records(pool: asyncpg.Pool, start: date, end: date) -> list[Record]:
    out: list[Record] = []
    for r in await pool.fetch(
            f"SELECT surrogate_id, id, channel_id, position, text, date, created_at, hidden_by_admin "
            f"FROM {Tables.MESSAGES} WHERE date BETWEEN $1 AND $2 AND text <> ''", start, end):
        out.append(Record(kind=KIND_MESSAGE, id=r['surrogate_id'], day=r['date'], source=f"ch:{r['channel_id']}",
                          text=r['text'], hidden=r['hidden_by_admin'], position=r['position'] or 0,
                          created_at=r['created_at'], tg_id=r['id']))
    for r in await pool.fetch(
            f"SELECT id, source, source_url, title, description, description_ru, venue, city, starts_at, created_at, hidden_by_admin "
            f"FROM {Tables.EVENTS} WHERE starts_at BETWEEN $1 AND $2", start, end):
        out.append(Record(kind=KIND_EVENT, id=r['id'], day=r['starts_at'], source=f"event:{(r['source'] or '').lower()}",
                          text=_event_text(r), hidden=r['hidden_by_admin'], created_at=r['created_at'],
                          city=r['city'], url=r['source_url']))
    return out


async def load_live(pool: asyncpg.Pool, start: date, end: date) -> dict[date, list[Live]]:
    by_day: dict[date, list[Live]] = {}
    for r in await pool.fetch(
            f"SELECT id, day, winner_kind, winner_id, loser_kind, loser_id, status, decided_by, reason "
            f"FROM {Tables.DEDUP_DECISIONS} WHERE status IN ('active', 'reverted') AND day BETWEEN $1 AND $2",
            start, end):
        by_day.setdefault(r['day'], []).append(Live(
            id=r['id'], loser=(r['loser_kind'], r['loser_id']), winner=(r['winner_kind'], r['winner_id']),
            status=r['status'], decided_by=r['decided_by'], reason=r['reason'] or ''))
    return by_day


async def load_day_decisions(pool: asyncpg.Pool, days: list[date]) -> dict[date, list[dict]]:
    """Решения (proposed/active/reverted) по датам: для журнала — что менялось относительно прошлого прогона."""
    by_day: dict[date, list[dict]] = {}
    if not days:
        return by_day
    for r in await pool.fetch(
            f"SELECT day, winner_kind, winner_id, loser_kind, loser_id, status, decided_by, model, reason "
            f"FROM {Tables.DEDUP_DECISIONS} WHERE day = ANY($1::date[]) AND status IN ('proposed', 'active', 'reverted')",
            days):
        by_day.setdefault(r['day'], []).append(dict(r))
    return by_day


async def load_fingerprints(pool: asyncpg.Pool, start: date, end: date) -> dict[date, str]:
    rows = await pool.fetch(f"SELECT day, fingerprint FROM {Tables.DEDUP_DATES} WHERE day BETWEEN $1 AND $2", start, end)
    return {r['day']: r['fingerprint'] for r in rows}


def _table(kind: str) -> str:
    return Tables.MESSAGES if kind == KIND_MESSAGE else Tables.EVENTS


def _id_col(kind: str) -> str:
    return 'surrogate_id' if kind == KIND_MESSAGE else 'id'


async def had_ai_run(pool: asyncpg.Pool) -> bool:
    return bool(await pool.fetchval(f"SELECT EXISTS (SELECT 1 FROM {Tables.DEDUP_RUNS} WHERE method = 'ai')"))


async def release_legacy_marks(pool: asyncpg.Pool) -> int:
    """Один раз при переходе на ai: снять автоскрытия старого дедупликатора (duplicate_of IS NOT NULL).
    Старый хук делал то же в начале каждого своего прогона; ручные скрытия (duplicate_of IS NULL) не трогаем."""
    res = await pool.execute(
        f"UPDATE {Tables.MESSAGES} SET hidden_by_admin = FALSE, duplicate_of = NULL "
        f"WHERE hidden_by_admin = TRUE AND duplicate_of IS NOT NULL")
    n = int(res.split()[-1])
    logger.info(f"DEDUP_LEGACY_RELEASED | rows={n}")
    return n


async def start_run(pool: asyncpg.Pool, method: str) -> int:
    return await pool.fetchval(f"INSERT INTO {Tables.DEDUP_RUNS} (method) VALUES ($1) RETURNING id", method)


async def finish_run(pool: asyncpg.Pool, run_id: int, status: str, stats: dict, error: str | None = None) -> None:
    await pool.execute(
        f"UPDATE {Tables.DEDUP_RUNS} SET finished_at = now(), status = $2, dates_total = $3, dates_processed = $4, "
        f"requests = $5, tokens_in = $6, tokens_out = $7, groups_found = $8, hidden = $9, unhidden = $10, error = $11 "
        f"WHERE id = $1",
        run_id, status, stats.get('dates_total', 0), stats.get('dates_processed', 0), stats.get('requests', 0),
        stats.get('tokens_in', 0), stats.get('tokens_out', 0), stats.get('groups_found', 0),
        stats.get('hidden', 0), stats.get('unhidden', 0), (error or '')[:500] or None)


async def apply_day(pool: asyncpg.Pool, plan: DayPlan, *, run_id: int | None, fingerprint: str, model: str) -> None:
    """Одна транзакция на дату: решения, скрытия и отпечаток применяются вместе или не применяются."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(f"DELETE FROM {Tables.DEDUP_DECISIONS} WHERE day = $1 AND status = 'proposed'", plan.day)
            if plan.method == 'ai':
                if plan.revert:
                    await conn.execute(f"UPDATE {Tables.DEDUP_DECISIONS} SET status = 'reverted', updated_at = now() "
                                       f"WHERE id = ANY($1::bigint[])", plan.revert)
                if plan.stale:
                    await conn.execute(f"UPDATE {Tables.DEDUP_DECISIONS} SET status = 'stale', updated_at = now() "
                                       f"WHERE id = ANY($1::bigint[])", plan.stale)
                for kind, rid in plan.unhide:
                    await conn.execute(f"UPDATE {_table(kind)} SET hidden_by_admin = FALSE "
                                       f"WHERE {_id_col(kind)} = $1 AND hidden_by_admin = TRUE", rid)
                for nd in plan.insert:
                    await conn.execute(f"DELETE FROM {Tables.DEDUP_DECISIONS} WHERE status = 'proposed' "
                                       f"AND loser_kind = $1 AND loser_id = $2", nd.loser.kind, nd.loser.id)
                    await conn.execute(
                        f"INSERT INTO {Tables.DEDUP_DECISIONS} (day, winner_kind, winner_id, loser_kind, loser_id, "
                        f"status, decided_by, model, reason, run_id) VALUES ($1, $2, $3, $4, $5, 'active', 'ai', $6, $7, $8)",
                        plan.day, nd.winner.kind, nd.winner.id, nd.loser.kind, nd.loser.id, model, nd.reason, run_id)
                for kind, rid in plan.hide:
                    await conn.execute(f"UPDATE {_table(kind)} SET hidden_by_admin = TRUE WHERE {_id_col(kind)} = $1", rid)
            else:
                for nd in plan.wanted:
                    await conn.execute(
                        f"INSERT INTO {Tables.DEDUP_DECISIONS} (day, winner_kind, winner_id, loser_kind, loser_id, "
                        f"status, decided_by, model, reason, run_id) VALUES ($1, $2, $3, $4, $5, 'proposed', 'ai', $6, $7, $8) "
                        f"ON CONFLICT DO NOTHING",
                        plan.day, nd.winner.kind, nd.winner.id, nd.loser.kind, nd.loser.id, model, nd.reason, run_id)
            await conn.execute(
                f"INSERT INTO {Tables.DEDUP_DATES} (day, fingerprint, processed_at, run_id) VALUES ($1, $2, now(), $3) "
                f"ON CONFLICT (day) DO UPDATE SET fingerprint = EXCLUDED.fingerprint, processed_at = now(), run_id = EXCLUDED.run_id",
                plan.day, fingerprint, run_id)
