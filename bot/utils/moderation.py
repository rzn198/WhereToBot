"""Операции админа над событиями и постами: запись решений в event_reviews и изменение events / messages.

Один модуль для «Пересмотра» (flow='review') и очередей «Неразобранные» (flow='no_date' / 'no_category'),
чтобы запись решения и правка основной таблицы всегда шли в одной транзакции.
"""
from __future__ import annotations

import json
from datetime import date, datetime

from loguru import logger

from bot.utils.refs import Ref, event_ref, is_event_ref, ref_id
from db.tables import Tables

FLOW_REVIEW = 'review'
FLOW_NO_DATE = 'no_date'
FLOW_NO_CATEGORY = 'no_category'

STATUS_OPEN = 'open'
STATUS_RESOLVED = 'resolved'
STATUS_APPLIED = 'applied'          # «пересмотрено»: категория записана в events / messages


# ---------- чтение записи ----------

async def load_record(db, ref: Ref) -> dict | None:
    if is_event_ref(ref):
        row = await db.fetchrow(f"SELECT * FROM {Tables.EVENTS} WHERE id = $1", ref_id(ref))
    else:
        row = await db.fetchrow(
            f"SELECT m.*, c.username AS channel_username, c.title AS channel_title "
            f"FROM {Tables.MESSAGES} m LEFT JOIN {Tables.CHANNELS} c ON c.channel_id = m.channel_id "
            f"WHERE m.surrogate_id = $1", ref_id(ref))
    return dict(row) if row else None


def build_review_values(ref: Ref, rec: dict) -> dict:
    """Поля записи пересмотра из строки events / messages."""
    if is_event_ref(ref):
        return {
            'kind': 'event', 'event_id': ref_id(ref), 'message_id': None,
            'title': rec.get('title'), 'description': rec.get('description'),
            'starts_at': rec.get('starts_at'), 'venue': rec.get('venue'),
            'source': rec.get('source'), 'source_url': rec.get('source_url'),
            'event_types': list(rec.get('event_types') or []),
        }
    text = rec.get('text') or ''
    username = rec.get('channel_username')
    first_line = next((ln.strip() for ln in text.splitlines() if ln.strip()), '')
    return {
        'kind': 'post', 'event_id': None, 'message_id': ref_id(ref),
        'title': first_line[:120], 'description': text,
        'starts_at': rec.get('date'), 'venue': None,
        'source': (f'@{username}' if username else rec.get('channel_title') or str(rec.get('channel_id'))),
        'source_url': f'https://t.me/{username}/{rec.get("id")}' if username and rec.get('id') else None,
        'event_types': list(rec.get('event_types') or []),
    }


def _json_default(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


# ---------- запись решения ----------

async def insert_decision(db, ref: Ref, rec: dict, *, flow: str, reason_code: str, status: str,
                          reason_text: str | None = None, correct_category: str | None = None,
                          correct_date: date | None = None) -> int | None:
    """INSERT в event_reviews со снимком записи. None — решение по этому элементу уже есть (конфликт индекса)."""
    v = build_review_values(ref, rec)
    snapshot = json.dumps(rec, default=_json_default, ensure_ascii=False)
    return await db.fetchval(
        f"""
        INSERT INTO {Tables.EVENT_REVIEWS}
            (flow, kind, event_id, message_id, reason_code, reason_text, correct_category, correct_date, status,
             resolved_at, title, description, starts_at, venue, source, source_url, event_types, snapshot)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::text,
                CASE WHEN $9::text = 'open' THEN NULL ELSE now() END,
                $10, $11, $12, $13, $14, $15, $16, $17::jsonb)
        ON CONFLICT DO NOTHING
        RETURNING id
        """,
        flow, v['kind'], v['event_id'], v['message_id'], reason_code, reason_text, correct_category, correct_date,
        status, v['title'], v['description'], v['starts_at'], v['venue'], v['source'], v['source_url'],
        v['event_types'], snapshot,
    )


# ---------- изменения основных таблиц ----------

async def _apply_category(db, ref: Ref, label: str) -> bool:
    """Категории записи заменяются целиком на одну выбранную. У события ставится category_manual."""
    if is_event_ref(ref):
        res = await db.execute(
            f"UPDATE {Tables.EVENTS} SET event_types = ARRAY[$2]::text[], category_manual = TRUE WHERE id = $1",
            ref_id(ref), label)
    else:
        res = await db.execute(
            f"UPDATE {Tables.MESSAGES} SET event_types = ARRAY[$2]::text[] WHERE surrogate_id = $1",
            ref_id(ref), label)
    return res.endswith(' 1')


async def _apply_date(db, ref: Ref, day: date) -> bool:
    """Дату ставим только записи без даты (гонка с другим источником не затирает уже заданную)."""
    if is_event_ref(ref):
        res = await db.execute(
            f"UPDATE {Tables.EVENTS} SET starts_at = $2 WHERE id = $1 AND starts_at IS NULL", ref_id(ref), day)
    else:
        res = await db.execute(
            f"UPDATE {Tables.MESSAGES} SET date = $2 WHERE surrogate_id = $1 AND date IS NULL", ref_id(ref), day)
    return res.endswith(' 1')


async def _apply_hide(db, ref: Ref) -> None:
    if is_event_ref(ref):
        await db.execute(f"UPDATE {Tables.EVENTS} SET hidden_by_admin = TRUE WHERE id = $1", ref_id(ref))
    else:
        await db.execute(
            f"UPDATE {Tables.MESSAGES} SET hidden_by_admin = TRUE, duplicate_of = NULL WHERE surrogate_id = $1",
            ref_id(ref))


# ---------- сценарии (транзакция: правка + запись решения) ----------

async def decide(pool, ref: Ref, *, flow: str, reason_code: str, status: str = STATUS_RESOLVED,
                 reason_text: str | None = None, correct_category: str | None = None,
                 correct_date: date | None = None) -> str:
    """Выполняет решение. Возвращает 'done' | 'missing' | 'changed' (дата уже задана другим путём).

    reason_code: set_date | rejected | set_category | skipped | hidden | wrong_category | wrong_date | manual.
    Основные таблицы меняют только set_date, set_category (и wrong_category с реальной категорией), hidden.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            rec = await load_record(conn, ref)
            if rec is None:
                return 'missing'
            if reason_code == 'set_date':
                if not await _apply_date(conn, ref, correct_date):
                    return 'changed'
            elif reason_code == 'set_category' or (reason_code == 'wrong_category' and status == STATUS_APPLIED):
                if not await _apply_category(conn, ref, correct_category):
                    return 'missing'
            elif reason_code == 'hidden':
                await _apply_hide(conn, ref)
            new_id = await insert_decision(
                conn, ref, rec, flow=flow, reason_code=reason_code, status=status, reason_text=reason_text,
                correct_category=correct_category, correct_date=correct_date)
    logger.info(f"MODERATION | flow={flow} | ref={ref} | reason={reason_code} | status={status} | "
                f"category={correct_category!r} | date={correct_date} | row={new_id}")
    return 'done'


# ---------- очереди «Неразобранные» ----------

def no_date_queue_sql() -> str:
    """Посты (и, на всякий случай, события) без даты, по которым в очереди ещё нет решения. $1 — город событий."""
    return f"""
        SELECT kind, id FROM (
            SELECT 0 AS ord, 'm' AS kind, m.surrogate_id AS id, m.created_at AS c
            FROM {Tables.MESSAGES} m
            WHERE m.date IS NULL AND NOT m.hidden_by_admin
              AND NOT EXISTS (SELECT 1 FROM {Tables.EVENT_REVIEWS} r
                              WHERE r.flow = 'no_date' AND r.message_id = m.surrogate_id)
            UNION ALL
            SELECT 1, 'e', e.id, e.created_at
            FROM {Tables.EVENTS} e
            WHERE e.starts_at IS NULL AND NOT e.hidden_by_admin AND e.city = $1
              AND NOT EXISTS (SELECT 1 FROM {Tables.EVENT_REVIEWS} r
                              WHERE r.flow = 'no_date' AND r.event_id = e.id)
        ) u
        ORDER BY c, ord, id
    """


def no_category_queue_sql() -> str:
    """Посты и события сегодняшнего дня и позже без категории, по которым в очереди ещё нет решения. $1 — город событий."""
    return f"""
        SELECT kind, id FROM (
            SELECT 0 AS ord, 'm' AS kind, m.surrogate_id AS id, m.date AS d, m.created_at AS c
            FROM {Tables.MESSAGES} m
            WHERE COALESCE(cardinality(m.event_types), 0) = 0 AND m.date >= CURRENT_DATE AND NOT m.hidden_by_admin
              AND NOT EXISTS (SELECT 1 FROM {Tables.EVENT_REVIEWS} r
                              WHERE r.flow = 'no_category' AND r.message_id = m.surrogate_id)
            UNION ALL
            SELECT 1, 'e', e.id, e.starts_at, e.created_at
            FROM {Tables.EVENTS} e
            WHERE COALESCE(cardinality(e.event_types), 0) = 0 AND e.starts_at >= CURRENT_DATE
              AND NOT e.hidden_by_admin AND e.city = $1
              AND NOT EXISTS (SELECT 1 FROM {Tables.EVENT_REVIEWS} r
                              WHERE r.flow = 'no_category' AND r.event_id = e.id)
        ) u
        ORDER BY d, ord, c, id
    """


async def load_queue(pool, flow: str, city: str) -> list[Ref]:
    sql = no_date_queue_sql() if flow == FLOW_NO_DATE else no_category_queue_sql()
    rows = await pool.fetch(sql, city)
    return [event_ref(r['id']) if r['kind'] == 'e' else r['id'] for r in rows]
