from __future__ import annotations

from loguru import logger

from event_collector.scrapers.base import EventItem
from event_collector.city_normalizer import normalize_city

EVENTS_TABLE = 'events'

_UPSERT_SQL = f"""
INSERT INTO {EVENTS_TABLE} (
    source, source_url, title, description,
    starts_at, venue, city, country,
    images, dedup_key, event_types
) VALUES (
    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11
)
ON CONFLICT (source_url) DO UPDATE SET
    title       = EXCLUDED.title,
    description = EXCLUDED.description,
    starts_at   = EXCLUDED.starts_at,
    venue       = EXCLUDED.venue,
    city        = EXCLUDED.city,
    country     = EXCLUDED.country,
    images      = EXCLUDED.images,
    dedup_key   = EXCLUDED.dedup_key,
    event_types = EXCLUDED.event_types,
    updated_at  = now()
"""


async def save_events(pool, items: list[EventItem]) -> int:

    if not items:
        logger.info("EVENT_SAVE | nothing to save")
        return 0

    source = items[0].source
    saved = 0

    async with pool.acquire() as conn:
        for it in items:
            try:
                await conn.execute(
                    _UPSERT_SQL,
                    it.source,
                    it.source_url,
                    it.title,
                    it.description,
                    it.starts_at.date() if it.starts_at else None,
                    it.venue,
                    normalize_city(it.city),
                    it.country,
                    it.images,
                    it.dedup_key,
                    it.event_types or [],
                )
                saved += 1
            except Exception as e:
                logger.error(
                    f"EVENT_SAVE_ERROR | source={it.source} | "
                    f"url={it.source_url} | {e}"
                )

    logger.info(f"EVENT_SAVED | source={source} | saved={saved}")
    return saved
