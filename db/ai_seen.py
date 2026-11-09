"""Журнал «пост уже оценён AI-парсингом» (таблица ai_parse_seen, миграция 009).

Зачем: просроченные посты в messages не сохраняются, проверка «уже сохранён» их не видит —
и каждый прогон коллектора отправлял их в AI заново. Запись в журнале пропускает пост, пока его текст не изменился.
Все функции best-effort: при ошибке БД (например, миграция ещё не применена) возвращают «не оценён»,
сбор сообщений не падает.
"""
from __future__ import annotations

import hashlib

import asyncpg
from loguru import logger

from db.tables import Tables


def text_hash(text: str) -> str:
    return hashlib.md5((text or '').encode('utf-8')).hexdigest()


async def load_seen(pool: asyncpg.Pool, channel_id: int, message_ids: list[int]) -> dict[tuple[int, int], str]:
    """{(message_id, position): text_hash} по постам канала."""
    if not message_ids:
        return {}
    try:
        rows = await pool.fetch(
            f"SELECT message_id, position, text_hash FROM {Tables.AI_PARSE_SEEN} "
            f"WHERE channel_id = $1 AND message_id = ANY($2::bigint[])",
            channel_id, list(message_ids))
        return {(r['message_id'], r['position']): r['text_hash'] for r in rows}
    except Exception as e:
        logger.warning(f"AI_SEEN_UNAVAILABLE | {type(e).__name__}: {e} — журнал оценённых постов не используется")
        return {}


async def mark_seen(pool: asyncpg.Pool, channel_id: int, message_id: int, position: int, text: str) -> None:
    try:
        await pool.execute(
            f"INSERT INTO {Tables.AI_PARSE_SEEN} (channel_id, message_id, position, text_hash) "
            f"VALUES ($1, $2, $3, $4) "
            f"ON CONFLICT (channel_id, message_id, position) DO UPDATE SET text_hash = EXCLUDED.text_hash, seen_at = now()",
            channel_id, message_id, position, text_hash(text))
    except Exception as e:
        logger.warning(f"AI_SEEN_WRITE_FAILED | {type(e).__name__}: {e}")


async def purge_old(pool: asyncpg.Pool, days: int) -> int:
    """Удаляет записи старше days дней. Вызывается из ежедневной очистки."""
    try:
        status = await pool.execute(
            f"DELETE FROM {Tables.AI_PARSE_SEEN} WHERE seen_at < now() - make_interval(days => $1)", days)
        return int(status.split()[-1])
    except Exception as e:
        logger.warning(f"AI_SEEN_PURGE_FAILED | {type(e).__name__}: {e}")
        return 0
