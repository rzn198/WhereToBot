"""Отчёт о переносах избранного (⭐) и скрытий (🙈) при дедупликации: CSV для админа.

run_id=None — все переносы (раздел «всё» в админке); run_id=N — только переносы, сделанные решениями прогона N.
Кодировка utf-8-sig: файл открывается в Excel без «кракозябр».
"""
from __future__ import annotations

import csv
import io

from db.tables import Tables

HEAD = ['Перенесено', 'Дата события', 'Пользователь', 'Итог на победителе', 'Что было у пользователя', 'Откат',
        'Победитель: источник', 'Победитель: id', 'Победитель: ссылка', 'Победитель: текст',
        'Проигравший: источник', 'Проигравший: id', 'Проигравший: ссылка', 'Проигравший: текст',
        'Решение', 'Прогон']

RESULT = {'fav': '⭐ избранное', 'hidden': '🙈 скрыто'}


def _was(m) -> str:
    parts = []
    if m['had_fav_loser']:
        parts.append('⭐ на проигравшем')
    if m['had_hid_loser']:
        parts.append('🙈 на проигравшем')
    if m['had_fav_winner']:
        parts.append('⭐ на победителе')
    if m['had_hid_winner']:
        parts.append('🙈 на победителе')
    return '; '.join(parts)


async def _lookup(pool, keys: set) -> dict:
    """(kind, id) → (источник, ссылка, текст); удалённые записи помечаются."""
    names = {r['channel_id']: r['username'] for r in await pool.fetch(f"SELECT channel_id, username FROM {Tables.CHANNELS}")}
    out: dict = {}
    mids = [i for k, i in keys if k == 'm']
    eids = [i for k, i in keys if k == 'e']
    if mids:
        for r in await pool.fetch(
                f"SELECT surrogate_id, id, channel_id, text FROM {Tables.MESSAGES} WHERE surrogate_id = ANY($1::bigint[])", mids):
            n = names.get(r['channel_id'])
            link = f"https://t.me/{n.lstrip('@')}/{r['id']}" if n else f"https://t.me/c/{r['channel_id']}/{r['id']}"
            out[('m', r['surrogate_id'])] = (('@' + n.lstrip('@')) if n else f"ch:{r['channel_id']}", link, r['text'] or '')
    if eids:
        for r in await pool.fetch(
                f"SELECT id, source, source_url, title, venue, description FROM {Tables.EVENTS} WHERE id = ANY($1::bigint[])", eids):
            out[('e', r['id'])] = (f"event:{r['source']}", r['source_url'] or '',
                                   '. '.join(x for x in (r['title'], r['venue'], r['description']) if x))
    for k in keys:
        out.setdefault(k, ('(запись удалена)', '', ''))
    return out


async def fetch_rows(pool, run_id: int | None = None) -> list[list]:
    where, args = ('', [])
    if run_id is not None:
        where, args = 'WHERE d.run_id = $1', [run_id]
    moves = await pool.fetch(
        f"SELECT m.*, d.run_id FROM {Tables.FAVORITE_MOVES} m "
        f"LEFT JOIN {Tables.DEDUP_DECISIONS} d ON d.id = m.decision_id {where} ORDER BY m.moved_at, m.id", *args)
    if not moves:
        return []
    keys = {(m['winner_kind'], m['winner_id']) for m in moves} | {(m['loser_kind'], m['loser_id']) for m in moves}
    info = await _lookup(pool, keys)
    rows = []
    for m in moves:
        ws, wl, wt = info[(m['winner_kind'], m['winner_id'])]
        ls, ll, lt = info[(m['loser_kind'], m['loser_id'])]
        rows.append([m['moved_at'].strftime('%Y-%m-%d %H:%M'), m['event_date'].isoformat(), m['user_id'],
                     RESULT.get(m['result'], m['result']), _was(m), 'да' if m['reverted'] else '',
                     ws, m['winner_id'], wl, wt, ls, m['loser_id'], ll, lt, m['decision_id'] or '', m['run_id'] or ''])
    return rows


def to_csv_bytes(rows: list[list]) -> bytes:
    buf = io.StringIO(newline='')
    w = csv.writer(buf)
    w.writerow(HEAD)
    w.writerows(rows)
    return buf.getvalue().encode('utf-8-sig')


async def build(pool, run_id: int | None = None) -> bytes | None:
    """CSV переносов либо None, если переносов нет."""
    rows = await fetch_rows(pool, run_id)
    return to_csv_bytes(rows) if rows else None
