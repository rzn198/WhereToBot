"""Перенос избранного (⭐) и скрытий пользователя (🙈) с проигравшего дубля на победителя и откат переноса.

Контракт (ТЗ Denis, ноябрь 2026): у скрытого проигравшего избранного не остаётся — оно уезжает на победителя,
пользователь видит его в «Избранное» и получает push по победителю. Запись проигравшего остаётся скрытой.

Матрица (проигравший / победитель → результат на победителе):
    ⭐ / ничего → ⭐          ⭐ / ⭐ → ⭐ (одна запись, added_at более ранний)     ⭐ / 🙈 → ⭐ (🙈 снимается)
    🙈 / ничего → 🙈          🙈 / ⭐ → ⭐ (скрытие проигравшего сбрасывается)      🙈 / 🙈 → 🙈
    ничего / что угодно → без изменений.
Если у пользователя на проигравшем есть и ⭐, и 🙈, побеждает ⭐.

Все функции работают на переданном соединении ВНУТРИ транзакции вызывающего (скрытие записи, смена решения и
перенос применяются вместе или не применяются). Модуль не зависит от deduplicator.* и telegram.
Записи адресуются парой (kind, id): 'm' — messages.surrogate_id, 'e' — events.id.
"""
from __future__ import annotations

from datetime import date, timedelta

import asyncpg
from loguru import logger

from db.tables import Tables

KIND_MESSAGE = 'm'
KIND_EVENT = 'e'
KEEP_AFTER_EVENT = timedelta(days=31)           # favorite_moves хранится до даты события + 1 месяц


def _fav(kind: str) -> tuple[str, str]:
    return (Tables.FAVORITES, 'msg_id') if kind == KIND_MESSAGE else (Tables.EVENT_FAVORITES, 'event_id')


def _hid(kind: str) -> tuple[str, str]:
    return (Tables.HIDDEN_EVENTS, 'msg_id') if kind == KIND_MESSAGE else (Tables.EVENT_HIDDEN, 'event_id')


async def transfer(conn: asyncpg.Connection, *, decision_id: int | None, day: date,
                   loser: tuple[str, int], winner: tuple[str, int]) -> int:
    """Перенести ⭐/🙈 всех пользователей с loser на winner. Возвращает число затронутых пользователей.
    Вызывать ПОСЛЕ того, как проигравший скрыт (hidden_by_admin = TRUE) в той же транзакции."""
    lk, li = loser
    wk, wi = winner
    if loser == winner:
        return 0
    lf_t, lf_c = _fav(lk)
    wf_t, wf_c = _fav(wk)
    lh_t, lh_c = _hid(lk)
    wh_t, wh_c = _hid(wk)

    fav_l = {r['user_id']: r['added_at'] for r in
             await conn.fetch(f"SELECT user_id, added_at FROM {lf_t} WHERE {lf_c} = $1", li)}
    hid_l = {r['user_id'] for r in await conn.fetch(f"SELECT user_id FROM {lh_t} WHERE {lh_c} = $1", li)}
    users = sorted(set(fav_l) | hid_l)
    if not users:
        return 0
    fav_w = {r['user_id']: r['added_at'] for r in
             await conn.fetch(f"SELECT user_id, added_at FROM {wf_t} WHERE {wf_c} = $1 AND user_id = ANY($2::bigint[])", wi, users)}
    hid_w = {r['user_id'] for r in
             await conn.fetch(f"SELECT user_id FROM {wh_t} WHERE {wh_c} = $1 AND user_id = ANY($2::bigint[])", wi, users)}

    for u in users:
        fl, hl, fw, hw = u in fav_l, u in hid_l, u in fav_w, u in hid_w
        if fl:
            if fw:
                if fav_l[u] < fav_w[u]:
                    await conn.execute(f"UPDATE {wf_t} SET added_at = $3 WHERE user_id = $1 AND {wf_c} = $2", u, wi, fav_l[u])
            else:
                await conn.execute(f"INSERT INTO {wf_t} (user_id, {wf_c}, added_at) VALUES ($1, $2, $3) "
                                   f"ON CONFLICT DO NOTHING", u, wi, fav_l[u])
            if hw:
                await conn.execute(f"DELETE FROM {wh_t} WHERE user_id = $1 AND {wh_c} = $2", u, wi)
            result = 'fav'
        else:                                   # только 🙈 на проигравшем
            if fw:
                result = 'fav'
            elif hw:
                result = 'hidden'
            else:
                await conn.execute(f"INSERT INTO {wh_t} (user_id, {wh_c}) VALUES ($1, $2) ON CONFLICT DO NOTHING", u, wi)
                result = 'hidden'
        await conn.execute(f"DELETE FROM {lf_t} WHERE user_id = $1 AND {lf_c} = $2", u, li)
        await conn.execute(f"DELETE FROM {lh_t} WHERE user_id = $1 AND {lh_c} = $2", u, li)
        await conn.execute(
            f"INSERT INTO {Tables.FAVORITE_MOVES} (decision_id, user_id, event_date, loser_kind, loser_id, winner_kind, "
            f"winner_id, result, had_fav_loser, loser_fav_added_at, had_fav_winner, winner_fav_added_at, had_hid_loser, "
            f"had_hid_winner) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14) "
            f"ON CONFLICT (decision_id, user_id) DO UPDATE SET result = EXCLUDED.result, moved_at = now(), reverted = FALSE, "
            f"reverted_at = NULL, winner_kind = EXCLUDED.winner_kind, winner_id = EXCLUDED.winner_id",
            decision_id, u, day, lk, li, wk, wi, result, fl, fav_l.get(u), fw, fav_w.get(u), hl, hw)
    logger.info(f"FAV_MOVED | decision={decision_id} | {lk}{li} -> {wk}{wi} | users={len(users)}")
    return len(users)


async def rollback(conn: asyncpg.Connection, decision_ids: list[int]) -> int:
    """Откатить переносы по решениям (проигравший воскрес: победитель пропал, скрыт, сменил дату, либо админ
    поменял решение). ⭐ и 🙈 возвращаются на проигравшего, с победителя снимается то, что добавил перенос.
    Если пользователь успел снять ⭐ с победителя сам, на проигравшего оно не возвращается.
    Возвращает число откаченных строк."""
    if not decision_ids:
        return 0
    moves = await conn.fetch(
        f"SELECT * FROM {Tables.FAVORITE_MOVES} WHERE decision_id = ANY($1::bigint[]) AND NOT reverted ORDER BY id",
        list(decision_ids))
    for m in moves:
        lk, li, wk, wi, u = m['loser_kind'], m['loser_id'], m['winner_kind'], m['winner_id'], m['user_id']
        lf_t, lf_c = _fav(lk)
        wf_t, wf_c = _fav(wk)
        lh_t, lh_c = _hid(lk)
        wh_t, wh_c = _hid(wk)
        if m['had_fav_loser']:
            if m['had_fav_winner']:
                restore = True
                if m['winner_fav_added_at'] is not None:
                    await conn.execute(f"UPDATE {wf_t} SET added_at = $3 WHERE user_id = $1 AND {wf_c} = $2",
                                       u, wi, m['winner_fav_added_at'])
            else:
                restore = bool(await conn.fetchval(
                    f"WITH d AS (DELETE FROM {wf_t} WHERE user_id = $1 AND {wf_c} = $2 RETURNING 1) SELECT count(*) FROM d",
                    u, wi))
            if restore:
                await conn.execute(f"INSERT INTO {lf_t} (user_id, {lf_c}, added_at) VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
                                   u, li, m['loser_fav_added_at'])
            if m['had_hid_winner']:
                await conn.execute(f"INSERT INTO {wh_t} (user_id, {wh_c}) VALUES ($1, $2) ON CONFLICT DO NOTHING", u, wi)
        elif m['result'] == 'hidden' and not m['had_hid_winner']:
            await conn.execute(f"DELETE FROM {wh_t} WHERE user_id = $1 AND {wh_c} = $2", u, wi)
        if m['had_hid_loser']:
            await conn.execute(f"INSERT INTO {lh_t} (user_id, {lh_c}) VALUES ($1, $2) ON CONFLICT DO NOTHING", u, li)
        await conn.execute(f"UPDATE {Tables.FAVORITE_MOVES} SET reverted = TRUE, reverted_at = now() WHERE id = $1", m['id'])
    if moves:
        logger.info(f"FAV_ROLLBACK | decisions={len(decision_ids)} rows={len(moves)}")
    return len(moves)


async def repoint_winner(conn: asyncpg.Connection, old: tuple[str, int], new: tuple[str, int]) -> None:
    """Победитель решений сменился (админ поменял местами): переносы, привязанные к old, теперь ведут на new."""
    await conn.execute(
        f"UPDATE {Tables.FAVORITE_MOVES} SET winner_kind = $1, winner_id = $2 "
        f"WHERE winner_kind = $3 AND winner_id = $4 AND NOT reverted", new[0], new[1], old[0], old[1])


async def purge_old(pool: asyncpg.Pool) -> int:
    """Удалить переносы, у которых дата события старше месяца."""
    res = await pool.execute(f"DELETE FROM {Tables.FAVORITE_MOVES} WHERE event_date < $1", date.today() - KEEP_AFTER_EVENT)
    n = int(res.split()[-1])
    if n:
        logger.info(f"FAV_MOVES_PURGED | rows={n}")
    return n


async def invariant_violations(pool: asyncpg.Pool) -> list[dict]:
    """FAV_INVARIANT: у скрытого действующим решением проигравшего (дата не в прошлом) не должно быть избранного.
    Возвращает пары для исправления: [{decision_id, day, loser:(k,i), winner:(k,i), users}]."""
    out: list[dict] = []
    for lk in (KIND_MESSAGE, KIND_EVENT):
        f_t, f_c = _fav(lk)
        tbl = Tables.MESSAGES if lk == KIND_MESSAGE else Tables.EVENTS
        idc = 'surrogate_id' if lk == KIND_MESSAGE else 'id'
        rows = await pool.fetch(
            f"SELECT d.id AS decision_id, d.day, d.loser_kind, d.loser_id, d.winner_kind, d.winner_id, "
            f"count(*) AS users FROM {Tables.DEDUP_DECISIONS} d "
            f"JOIN {tbl} t ON t.{idc} = d.loser_id "
            f"JOIN {f_t} f ON f.{f_c} = d.loser_id "
            f"WHERE d.status = 'active' AND d.loser_kind = $1 AND t.hidden_by_admin = TRUE AND d.day >= CURRENT_DATE "
            f"GROUP BY d.id, d.day, d.loser_kind, d.loser_id, d.winner_kind, d.winner_id", lk)
        for r in rows:
            out.append(dict(decision_id=r['decision_id'], day=r['day'], loser=(r['loser_kind'], r['loser_id']),
                            winner=(r['winner_kind'], r['winner_id']), users=r['users']))
    return out


async def heal_invariant(pool: asyncpg.Pool) -> list[dict]:
    """Самопроверка после прогона дедупа: если у скрытого проигравшего всё же остались ⭐ (гонка, старый код),
    переносим их на победителя. Возвращает найденные нарушения (пустой список — всё чисто)."""
    bad = await invariant_violations(pool)
    for b in bad:
        logger.error(f"FAV_INVARIANT | decision={b['decision_id']} | loser={b['loser'][0]}{b['loser'][1]} "
                     f"winner={b['winner'][0]}{b['winner'][1]} | users={b['users']} — переношу на победителя")
        async with pool.acquire() as conn:
            async with conn.transaction():
                await transfer(conn, decision_id=b['decision_id'], day=b['day'], loser=b['loser'], winner=b['winner'])
    return bad


async def resolve_winner(pool_or_conn, kind: str, rid: int, *, max_hops: int = 5) -> tuple[str, int]:
    """Запись, которую надо показать/добавить в избранное вместо скрытого проигравшего: идём по действующим
    решениям к победителю. Если запись не скрыта решением — возвращается как есть."""
    cur = (kind, rid)
    for _ in range(max_hops):
        row = await pool_or_conn.fetchrow(
            f"SELECT winner_kind, winner_id FROM {Tables.DEDUP_DECISIONS} "
            f"WHERE loser_kind = $1 AND loser_id = $2 AND status = 'active'", cur[0], cur[1])
        if row is None:
            return cur
        cur = (row['winner_kind'], row['winner_id'])
    return cur
