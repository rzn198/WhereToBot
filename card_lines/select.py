"""Какие записи получают ИИ-строку.

Берутся будущие записи окна категорий (события сайтов города и посты каналов), которые:
  * не скрыты админом и не проигравшие в живом решении дедупликатора (proposed/active);
  * при after_dedup — их дата уже обработана дедупликатором ПОСЛЕ появления записи (как у перевода): дубли не тратят запросы;
  * имеют профиль со включённым ИИ по основной (первой) категории;
  * не имеют актуальной строки: нет в card_lines или хэш входных данных / версий промптов изменился.
Ближайшие даты идут первыми.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date

from card_lines.profiles import profile_for
from card_lines.settings import CardLineSettings
from card_lines.text import Candidate, src_hash
from db.tables import Tables


@dataclass
class Selection:
    todo: list[Candidate] = field(default_factory=list)
    total: int = 0                  # будущих записей в выборке
    no_profile: int = 0             # у основной категории нет профиля или он без ИИ
    up_to_date: int = 0             # актуальная строка уже есть
    by_profile: Counter = field(default_factory=Counter)


def candidates_sql() -> str:
    """$1 город событий, $2 первый день, $3 последний, $4 only_after_dedup."""
    return f"""
        SELECT 'e'::text AS kind, e.id::bigint AS id, e.title AS title, coalesce(e.description, '') AS body,
               coalesce(e.venue, '') AS venue, e.event_types, e.starts_at AS d, cl.src_hash
        FROM {Tables.EVENTS} e
        LEFT JOIN {Tables.CARD_LINES} cl ON cl.kind = 'e' AND cl.ref_id = e.id
        WHERE e.city = $1 AND NOT e.hidden_by_admin AND e.starts_at BETWEEN $2::date AND $3::date
          AND NOT EXISTS (SELECT 1 FROM {Tables.DEDUP_DECISIONS} d
                          WHERE d.loser_kind = 'e' AND d.loser_id = e.id AND d.status IN ('proposed', 'active'))
          AND ($4::boolean = false OR EXISTS (SELECT 1 FROM {Tables.DEDUP_DATES} dd
                          WHERE dd.day = e.starts_at AND dd.processed_at >= e.created_at))
        UNION ALL
        SELECT 'm', m.surrogate_id, '', coalesce(m.text, ''), '', m.event_types, m.date, cl.src_hash
        FROM {Tables.MESSAGES} m
        LEFT JOIN {Tables.CARD_LINES} cl ON cl.kind = 'm' AND cl.ref_id = m.surrogate_id
        WHERE NOT m.hidden_by_admin AND m.date BETWEEN $2::date AND $3::date
          AND NOT EXISTS (SELECT 1 FROM {Tables.DEDUP_DECISIONS} d
                          WHERE d.loser_kind = 'm' AND d.loser_id = m.surrogate_id AND d.status IN ('proposed', 'active'))
          AND ($4::boolean = false OR EXISTS (SELECT 1 FROM {Tables.DEDUP_DATES} dd
                          WHERE dd.day = m.date AND dd.processed_at >= m.created_at))
        ORDER BY d, kind, id
    """


async def select_candidates(pool, st: CardLineSettings, *, today: date, horizon_days: int, city: str,
                            include_done: bool = False) -> Selection:
    """include_done=True — вернуть и записи с актуальной строкой (для скрипта сравнения моделей)."""
    from datetime import timedelta
    rows = await pool.fetch(candidates_sql(), city, today, today + timedelta(days=horizon_days), st.after_dedup)
    sel = Selection(total=len(rows))
    for r in rows:
        prof = profile_for(r['event_types'], st.profiles)
        if prof is None:
            sel.no_profile += 1
            continue
        c = Candidate(kind=r['kind'], id=r['id'], title=r['title'] or '', body=r['body'], venue=r['venue'], profile=prof)
        h = src_hash(c, st.input_chars, st.title_max)
        if r['src_hash'] == h and not include_done:
            sel.up_to_date += 1
            continue
        sel.todo.append(Candidate(c.kind, c.id, c.title, c.body, c.venue, c.profile, h))
        sel.by_profile[prof.name] += 1
    return sel
