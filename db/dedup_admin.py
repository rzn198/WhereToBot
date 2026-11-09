"""Ручные действия админа над решениями дедупликатора (экраны «Скрытые события» и «Дубли»).

Каждое действие меняет `hidden_by_admin` у записей и `dedup_decisions` ОДНОЙ транзакцией, чтобы AI на следующем
прогоне не спорил с админом:
  * restore_loser  — «Вернуть»: проигравший снова виден, решение → reverted (AI больше его не скрывает);
  * swap_decision  — «Поменять местами»: проигравший становится победителем, бывший победитель скрыт;
  * show_record    — «Показать для всех»: снять скрытие; если запись скрыта решением AI — решение → reverted;
  * hide_record    — «Скрыть для всех»: ручное скрытие без решения (дедупликатор такую запись сам не вернёт).

Записи адресуются парой (kind, id): 'm' — messages.surrogate_id, 'e' — events.id.
Модуль не зависит от deduplicator.* и telegram: им пользуются и бот, и командная строка.
"""
from __future__ import annotations

from dataclasses import dataclass

import asyncpg
from loguru import logger

from db.tables import Tables

KIND_MESSAGE = 'm'
KIND_EVENT = 'e'
LIVE = ('active', 'reverted')


@dataclass(frozen=True)
class Outcome:
    ok: bool
    text: str


def _table(kind: str) -> str:
    return Tables.MESSAGES if kind == KIND_MESSAGE else Tables.EVENTS


def _id_col(kind: str) -> str:
    return 'surrogate_id' if kind == KIND_MESSAGE else 'id'


def parse_ref(raw: str) -> tuple[str, int] | None:
    """'m123' / 'e45' → ('m', 123) / ('e', 45); мусор → None."""
    if not raw or raw[0] not in (KIND_MESSAGE, KIND_EVENT) or not raw[1:].isdigit():
        return None
    return raw[0], int(raw[1:])


def make_ref(kind: str, rid: int) -> str:
    return f"{kind}{rid}"


# ---------------------------------------------------------------------------------------------- чтение

async def record_info(pool: asyncpg.Pool, kind: str, rid: int) -> dict | None:
    """Запись для показа админу: kind, id, day, text, hidden, position, source (подпись источника)."""
    if kind == KIND_MESSAGE:
        r = await pool.fetchrow(
            f"SELECT m.surrogate_id, m.text, m.date, m.position, m.hidden_by_admin, m.channel_id, c.username "
            f"FROM {Tables.MESSAGES} m LEFT JOIN {Tables.CHANNELS} c ON c.channel_id = m.channel_id "
            f"WHERE m.surrogate_id = $1", rid)
        if r is None:
            return None
        label = f"@{r['username']}" if r['username'] else f"ch:{r['channel_id']}"
        return dict(kind=kind, id=rid, day=r['date'], text=r['text'] or '', hidden=r['hidden_by_admin'],
                    position=r['position'] or 0, source=label)
    r = await pool.fetchrow(
        f"SELECT id, source, title, venue, description, starts_at, hidden_by_admin FROM {Tables.EVENTS} WHERE id = $1", rid)
    if r is None:
        return None
    text = '. '.join(p.strip() for p in (r['title'] or '', r['venue'] or '', (r['description'] or '')[:400]) if p and p.strip())
    return dict(kind=kind, id=rid, day=r['starts_at'], text=text, hidden=r['hidden_by_admin'], position=0,
                source=f"event:{(r['source'] or '').lower()}")


async def decision_for_loser(pool: asyncpg.Pool, kind: str, rid: int) -> dict | None:
    """Действующее решение, по которому запись скрыта (status active), либо None."""
    r = await pool.fetchrow(
        f"SELECT * FROM {Tables.DEDUP_DECISIONS} WHERE loser_kind = $1 AND loser_id = $2 AND status = 'active'", kind, rid)
    return dict(r) if r else None


async def dependents_count(pool: asyncpg.Pool, kind: str, rid: int) -> int:
    """Сколько записей скрыто в пользу этой (она — победитель действующих решений)."""
    return await pool.fetchval(
        f"SELECT count(*) FROM {Tables.DEDUP_DECISIONS} "
        f"WHERE winner_kind = $1 AND winner_id = $2 AND status = 'active'", kind, rid)


async def list_decision_ids(pool: asyncpg.Pool, status: str = 'active') -> list[int]:
    """Решения для экрана «Дубли»: от сегодня и позже, сначала свежие."""
    rows = await pool.fetch(
        f"SELECT id FROM {Tables.DEDUP_DECISIONS} WHERE status = $1 AND day >= CURRENT_DATE "
        f"ORDER BY created_at DESC, id DESC", status)
    return [r['id'] for r in rows]


async def get_decision(pool: asyncpg.Pool, decision_id: int) -> dict | None:
    """Решение вместе с обеими записями: ключи decision, winner, loser (winner/loser могут быть None, если запись удалена)."""
    d = await pool.fetchrow(f"SELECT * FROM {Tables.DEDUP_DECISIONS} WHERE id = $1", decision_id)
    if d is None:
        return None
    return dict(decision=dict(d),
                winner=await record_info(pool, d['winner_kind'], d['winner_id']),
                loser=await record_info(pool, d['loser_kind'], d['loser_id']))


async def can_swap(pool: asyncpg.Pool, decision: dict, winner: dict | None, loser: dict | None) -> bool:
    """Поменять местами можно, если обе записи есть, бывший победитель не строка дайджеста (её скрывать нельзя)
    и сам не числится проигравшим в другом действующем решении (на проигравшего — не больше одного решения)."""
    if decision['status'] != 'active' or winner is None or loser is None:
        return False
    if winner['kind'] == KIND_MESSAGE and winner['position'] > 0:
        return False
    other = await pool.fetchval(
        f"SELECT 1 FROM {Tables.DEDUP_DECISIONS} WHERE loser_kind = $1 AND loser_id = $2 "
        f"AND status = ANY($3::text[]) AND id <> $4", decision['winner_kind'], decision['winner_id'], list(LIVE), decision['id'])
    return other is None


# ---------------------------------------------------------------------------------------------- действия

async def restore_loser(pool: asyncpg.Pool, decision_id: int, admin_id: int | None = None) -> Outcome:
    """«Вернуть»: оба видны, AI больше не скрывает проигравшего."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            d = await conn.fetchrow(f"SELECT * FROM {Tables.DEDUP_DECISIONS} WHERE id = $1 FOR UPDATE", decision_id)
            if d is None or d['status'] != 'active':
                return Outcome(False, 'Решение уже изменилось')
            await conn.execute(
                f"UPDATE {_table(d['loser_kind'])} SET hidden_by_admin = FALSE WHERE {_id_col(d['loser_kind'])} = $1",
                d['loser_id'])
            await conn.execute(
                f"UPDATE {Tables.DEDUP_DECISIONS} SET status = 'reverted', updated_at = now() WHERE id = $1", decision_id)
    _log('restore', admin_id, d)
    return Outcome(True, 'Возвращено')


async def swap_decision(pool: asyncpg.Pool, decision_id: int, admin_id: int | None = None) -> Outcome:
    """«Поменять местами»: проигравший → победитель (виден), бывший победитель → скрыт.
    Остальные проигравшие бывшего победителя переходят под нового. Новое решение — от админа (AI его не пересчитывает)."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            d = await conn.fetchrow(f"SELECT * FROM {Tables.DEDUP_DECISIONS} WHERE id = $1 FOR UPDATE", decision_id)
            if d is None or d['status'] != 'active':
                return Outcome(False, 'Решение уже изменилось')
            wk, wi, lk, li = d['winner_kind'], d['winner_id'], d['loser_kind'], d['loser_id']
            if wk == KIND_MESSAGE:
                pos = await conn.fetchval(f"SELECT position FROM {Tables.MESSAGES} WHERE surrogate_id = $1", wi)
                if pos is None:
                    return Outcome(False, 'Победитель удалён, поменять местами нельзя')
                if pos > 0:
                    return Outcome(False, 'Победитель — строка дайджеста, её нельзя скрывать')
            elif await conn.fetchval(f"SELECT 1 FROM {Tables.EVENTS} WHERE id = $1", wi) is None:
                return Outcome(False, 'Победитель удалён, поменять местами нельзя')
            if await conn.fetchval(
                    f"SELECT 1 FROM {Tables.DEDUP_DECISIONS} WHERE loser_kind = $1 AND loser_id = $2 "
                    f"AND status = ANY($3::text[]) AND id <> $4", wk, wi, list(LIVE), decision_id):
                return Outcome(False, 'Победитель сам скрыт другим решением, поменять местами нельзя')

            await conn.execute(f"UPDATE {Tables.DEDUP_DECISIONS} SET status = 'stale', updated_at = now() WHERE id = $1", decision_id)
            # прочие проигравшие бывшего победителя → под нового победителя
            await conn.execute(
                f"UPDATE {Tables.DEDUP_DECISIONS} SET winner_kind = $1, winner_id = $2, updated_at = now() "
                f"WHERE winner_kind = $3 AND winner_id = $4 AND status = ANY($5::text[]) AND id <> $6",
                lk, li, wk, wi, list(LIVE), decision_id)
            await conn.execute(
                f"DELETE FROM {Tables.DEDUP_DECISIONS} WHERE status = 'proposed' AND loser_kind = $1 AND loser_id = $2", wk, wi)
            await conn.execute(
                f"INSERT INTO {Tables.DEDUP_DECISIONS} (day, winner_kind, winner_id, loser_kind, loser_id, status, decided_by, reason) "
                f"VALUES ($1, $2, $3, $4, $5, 'active', 'admin', 'вручную: поменяли местами')",
                d['day'], lk, li, wk, wi)
            await conn.execute(f"UPDATE {_table(wk)} SET hidden_by_admin = TRUE WHERE {_id_col(wk)} = $1", wi)
            await conn.execute(f"UPDATE {_table(lk)} SET hidden_by_admin = FALSE WHERE {_id_col(lk)} = $1", li)
    _log('swap', admin_id, d)
    return Outcome(True, 'Поменяли местами')


async def show_record(pool: asyncpg.Pool, kind: str, rid: int, admin_id: int | None = None) -> Outcome:
    """«Показать для всех». Если запись скрыта решением AI — решение → reverted, чтобы AI не скрыл её снова."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            n = await conn.execute(f"UPDATE {_table(kind)} SET hidden_by_admin = FALSE WHERE {_id_col(kind)} = $1", rid)
            if n.endswith(' 0'):
                return Outcome(False, 'Запись не найдена')
            await conn.execute(
                f"UPDATE {Tables.DEDUP_DECISIONS} SET status = 'reverted', updated_at = now() "
                f"WHERE loser_kind = $1 AND loser_id = $2 AND status = 'active'", kind, rid)
    logger.info(f"DEDUP_ADMIN_ACTION | admin_id={admin_id} | action=show | record={make_ref(kind, rid)}")
    return Outcome(True, 'Показано')


async def hide_record(pool: asyncpg.Pool, kind: str, rid: int, admin_id: int | None = None) -> Outcome:
    """«Скрыть для всех»: ручное скрытие. Решение не создаётся — дедупликатор такую запись не трогает."""
    n = await pool.execute(f"UPDATE {_table(kind)} SET hidden_by_admin = TRUE WHERE {_id_col(kind)} = $1", rid)
    if n.endswith(' 0'):
        return Outcome(False, 'Запись не найдена')
    logger.info(f"DEDUP_ADMIN_ACTION | admin_id={admin_id} | action=hide | record={make_ref(kind, rid)}")
    return Outcome(True, 'Скрыто')


def _log(action: str, admin_id: int | None, d) -> None:
    logger.info(f"DEDUP_ADMIN_ACTION | admin_id={admin_id} | action={action} | decision_id={d['id']} | "
                f"winner={d['winner_kind']}{d['winner_id']} | loser={d['loser_kind']}{d['loser_id']}")


# ---------------------------------------------------------------------------------------------- откат

async def release_ai_hidden(pool: asyncpg.Pool, *, include_admin: bool = False, apply: bool = False) -> dict:
    """Вернуть всё, что скрыли решения AI (status active, decided_by='ai'; с include_admin — и решения админа).
    apply=False — только посчитать. Решения → stale. Записи, скрытые вручную (без решения), не трогаем."""
    who = "('ai', 'admin')" if include_admin else "('ai')"
    async with pool.acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch(
                f"SELECT id, day, loser_kind, loser_id FROM {Tables.DEDUP_DECISIONS} "
                f"WHERE status = 'active' AND decided_by IN {who} FOR UPDATE")
            out = dict(decisions=len(rows), posts=sum(1 for r in rows if r['loser_kind'] == KIND_MESSAGE),
                       events=sum(1 for r in rows if r['loser_kind'] == KIND_EVENT),
                       days=sorted({r['day'] for r in rows}), applied=False)
            if apply and rows:
                for kind in (KIND_MESSAGE, KIND_EVENT):
                    ids = [r['loser_id'] for r in rows if r['loser_kind'] == kind]
                    if ids:
                        await conn.execute(
                            f"UPDATE {_table(kind)} SET hidden_by_admin = FALSE WHERE {_id_col(kind)} = ANY($1::bigint[])", ids)
                await conn.execute(
                    f"UPDATE {Tables.DEDUP_DECISIONS} SET status = 'stale', updated_at = now() WHERE id = ANY($1::bigint[])",
                    [r['id'] for r in rows])
                out['applied'] = True
    if out['applied']:
        logger.info(f"DEDUP_RELEASE | posts={out['posts']} events={out['events']} decisions={out['decisions']}")
    return out
