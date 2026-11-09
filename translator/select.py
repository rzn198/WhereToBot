"""Какие события переводить: победители дедупликации из списка источников.

Событие берётся на перевод, если одновременно:
  * источник (events.source) входит в translation.sources;
  * есть описание, а перевода нет (description_ru IS NULL; saver сам обнуляет его при смене описания);
  * не скрыто и не проигравший в живом решении (proposed/active) — «победитель» или одиночка; решение reverted
    (админ вернул) считается победителем;
  * его дата уже обработана дедупликатором ПОСЛЕ появления события (dedup_dates.processed_at >= created_at):
    свежесобранные события не переводятся до проверки на дубли;
  * при only_upcoming — дата не в прошлом. Ближайшие события идут первыми.
"""
from __future__ import annotations

from datetime import date

import asyncpg

from db.tables import Tables


async def select_candidates(pool: asyncpg.Pool, sources: list[str], *, only_upcoming: bool = True,
                            today: date | None = None, limit: int | None = None) -> list[asyncpg.Record]:
    if not sources:
        return []
    today = today or date.today()
    sql = f"""
        SELECT e.id, e.source, e.source_url, e.description, e.starts_at
        FROM {Tables.EVENTS} e
        WHERE lower(e.source) = ANY($1::text[])
          AND e.description IS NOT NULL AND btrim(e.description) <> ''
          AND e.description_ru IS NULL
          AND NOT e.hidden_by_admin
          AND ($2::boolean = false OR e.starts_at >= $3)
          AND NOT EXISTS (
                SELECT 1 FROM {Tables.DEDUP_DECISIONS} d
                WHERE d.loser_kind = 'e' AND d.loser_id = e.id AND d.status IN ('proposed', 'active'))
          AND EXISTS (
                SELECT 1 FROM {Tables.DEDUP_DATES} dd
                WHERE dd.day = e.starts_at AND dd.processed_at >= e.created_at)
        ORDER BY e.starts_at, e.id
    """
    args: list = [[s.lower() for s in sources], only_upcoming, today]
    if limit:
        sql += " LIMIT $4"
        args.append(limit)
    return await pool.fetch(sql, *args)
