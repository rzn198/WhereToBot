"""CSV-файлы полного отчёта дедупликатора для админа в Telegram.

changes — что изменил именно этот прогон (ai: скрыто / возвращено; shadow: предложено скрыть).
current — снимок всех действующих решений дедупа (proposed / active) на момент прогона.
Кодировка utf-8-sig, чтобы файл открывался в Excel без «кракозябр».
"""
from __future__ import annotations

import csv
import io

from db.tables import Tables
from deduplicator.audit import _link
from deduplicator.priority import PriorityResolver

CHANGES_HEAD = ['Дата', 'Действие', 'Происхождение', 'Победитель: источник', 'Победитель: id', 'Победитель: ссылка',
                'Победитель: текст', 'Проигравший: источник', 'Проигравший: id', 'Проигравший: ссылка',
                'Проигравший: текст', 'Причина']
CURRENT_HEAD = ['Дата', 'Статус', 'Кто решил', 'Модель', 'Победитель: источник', 'Победитель: id', 'Победитель: ссылка',
                'Победитель: текст', 'Проигравший: источник', 'Проигравший: id', 'Проигравший: ссылка',
                'Проигравший: текст', 'Причина']

VIA = {'ai_group': 'ИИ (группа)', 'exact_copy': 'точная копия', 'same_source': 'копия того же источника'}


def to_csv(head: list[str], rows: list[list]) -> bytes:
    buf = io.StringIO(newline='')
    w = csv.writer(buf)
    w.writerow(head)
    w.writerows(rows)
    return buf.getvalue().encode('utf-8-sig')


async def _channel_names(pool) -> dict:
    rows = await pool.fetch(f"SELECT channel_id, username FROM {Tables.CHANNELS}")
    return {r['channel_id']: r['username'] for r in rows}


async def _lookup(pool, keys: set, names: dict) -> dict:
    """(kind, id) → (источник, ссылка, текст) для записей из БД; удалённые помечаются."""
    mids = [i for k, i in keys if k == 'm']
    eids = [i for k, i in keys if k == 'e']
    out: dict = {}
    if mids:
        for r in await pool.fetch(
                f"SELECT surrogate_id, id, channel_id, text FROM {Tables.MESSAGES} WHERE surrogate_id = ANY($1::bigint[])", mids):
            n = names.get(r['channel_id'])
            link = f"https://t.me/{n.lstrip('@')}/{r['id']}" if n else f"https://t.me/c/{r['channel_id']}/{r['id']}"
            out[('m', r['surrogate_id'])] = (('@' + n.lstrip('@')) if n else f"ch:{r['channel_id']}", link, r['text'] or '')
    if eids:
        for r in await pool.fetch(
                f"SELECT id, source, source_url, title, venue, description FROM {Tables.EVENTS} WHERE id = ANY($1::bigint[])", eids):
            text = '. '.join(x for x in (r['title'], r['venue'], r['description']) if x)
            out[('e', r['id'])] = (f"event:{r['source']}", r['source_url'] or '', text)
    for k in keys:
        out.setdefault(k, ('(запись удалена)', '', ''))
    return out


def _rec(r, resolver) -> tuple:
    return (resolver.label(r), r.id, _link(r, resolver) or '', r.text)


async def build_changes(pool, res, method: str) -> bytes | None:
    """Изменения прогона. None — нечего выгружать."""
    resolver = res.resolver or PriorityResolver()
    rows: list[list] = []
    if method == 'ai':
        for p in res.plans:
            for nd in p.insert:
                w, l = _rec(nd.winner, resolver), _rec(nd.loser, resolver)
                rows.append([p.day.isoformat(), 'скрыто', VIA.get(nd.via, nd.via), *w, *l, nd.reason])
        unhide = {k for p in res.plans for k in p.unhide}
        if unhide:
            info = await _lookup(pool, unhide, await _channel_names(pool))
            for p in res.plans:
                for k in p.unhide:
                    src, link, text = info[k]
                    rows.append([p.day.isoformat(), 'возвращено (решение снято)', '', '', '', '', '', src, k[1], link, text, ''])
    else:
        for p in res.plans:
            for nd in p.wanted:
                w, l = _rec(nd.winner, resolver), _rec(nd.loser, resolver)
                rows.append([p.day.isoformat(), 'предложено скрыть (shadow)', VIA.get(nd.via, nd.via), *w, *l, nd.reason])
    return to_csv(CHANGES_HEAD, rows) if rows else None


async def build_current(pool) -> bytes | None:
    """Снимок действующих решений (proposed / active) на сегодня и позже."""
    decs = await pool.fetch(
        f"SELECT day, winner_kind, winner_id, loser_kind, loser_id, status, decided_by, model, reason "
        f"FROM {Tables.DEDUP_DECISIONS} WHERE status IN ('proposed', 'active') AND day >= CURRENT_DATE "
        f"ORDER BY day, winner_kind, winner_id, loser_id")
    if not decs:
        return None
    keys = {(d['winner_kind'], d['winner_id']) for d in decs} | {(d['loser_kind'], d['loser_id']) for d in decs}
    info = await _lookup(pool, keys, await _channel_names(pool))
    rows = []
    for d in decs:
        ws, wl, wt = info[(d['winner_kind'], d['winner_id'])]
        ls, ll, lt = info[(d['loser_kind'], d['loser_id'])]
        rows.append([d['day'].isoformat(), d['status'], d['decided_by'], d['model'] or '', ws, d['winner_id'], wl, wt,
                     ls, d['loser_id'], ll, lt, d['reason'] or ''])
    return to_csv(CURRENT_HEAD, rows)
