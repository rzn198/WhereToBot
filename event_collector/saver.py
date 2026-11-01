from __future__ import annotations

from loguru import logger

from event_collector.scrapers.base import EventItem
from event_collector.city_normalizer import normalize_city
from event_collector.retention import get_window, in_window

EVENTS_TABLE = 'events'


def _first_image(images) -> str | None:
    if not images:
        return None
    if isinstance(images, str):
        return images.strip() or None
    if isinstance(images, (list, tuple)):
        for u in images:
            if u:
                return str(u)
        return None
    return None

_NEW_DESC = "COALESCE(EXCLUDED.description, {t}.description)"
_NEW_IMG = "COALESCE(EXCLUDED.images, {t}.images)"

_UPSERT_SQL = f"""
INSERT INTO {EVENTS_TABLE} (
    source, source_url, title, description,
    starts_at, venue, city, country,
    images, dedup_key, event_types
) VALUES (
    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11
)
ON CONFLICT (source_url) DO UPDATE SET
    title          = EXCLUDED.title,
    description    = {_NEW_DESC.format(t=EVENTS_TABLE)},
    starts_at      = EXCLUDED.starts_at,
    venue          = COALESCE(EXCLUDED.venue, {EVENTS_TABLE}.venue),
    city           = COALESCE(EXCLUDED.city, {EVENTS_TABLE}.city),
    country        = COALESCE(EXCLUDED.country, {EVENTS_TABLE}.country),
    images         = {_NEW_IMG.format(t=EVENTS_TABLE)},
    dedup_key      = EXCLUDED.dedup_key,
    event_types    = EXCLUDED.event_types,
    description_ru = CASE
        WHEN {_NEW_DESC.format(t=EVENTS_TABLE)} IS DISTINCT FROM {EVENTS_TABLE}.description
        THEN NULL ELSE {EVENTS_TABLE}.description_ru END,
    image_file_id  = CASE
        WHEN {_NEW_IMG.format(t=EVENTS_TABLE)} IS DISTINCT FROM {EVENTS_TABLE}.images
        THEN NULL ELSE {EVENTS_TABLE}.image_file_id END,
    updated_at     = now()
WHERE (
    {EVENTS_TABLE}.title, {EVENTS_TABLE}.description, {EVENTS_TABLE}.starts_at,
    {EVENTS_TABLE}.venue, {EVENTS_TABLE}.city, {EVENTS_TABLE}.country,
    {EVENTS_TABLE}.images, {EVENTS_TABLE}.dedup_key, {EVENTS_TABLE}.event_types
) IS DISTINCT FROM (
    EXCLUDED.title, {_NEW_DESC.format(t=EVENTS_TABLE)}, EXCLUDED.starts_at,
    COALESCE(EXCLUDED.venue, {EVENTS_TABLE}.venue),
    COALESCE(EXCLUDED.city, {EVENTS_TABLE}.city),
    COALESCE(EXCLUDED.country, {EVENTS_TABLE}.country),
    {_NEW_IMG.format(t=EVENTS_TABLE)}, EXCLUDED.dedup_key, EXCLUDED.event_types
)
RETURNING (xmax = 0) AS inserted
"""


async def save_events(pool, items: list[EventItem]) -> int:

    if not items:
        logger.info("EVENT_SAVE | nothing to save")
        return 0

    source = items[0].source
    inserted = updated = unchanged = 0

    past_days, future_days = get_window()
    kept = []
    skipped = 0
    for it in items:
        d = it.starts_at.date() if it.starts_at else None
        if in_window(d, past_days=past_days, future_days=future_days):
            kept.append(it)
        else:
            skipped += 1
    if skipped:
        logger.info(
            f"EVENT_SKIPPED_OUT_OF_WINDOW | source={source} | skipped={skipped}"
        )
    items = kept
    if not items:
        logger.info(f"EVENT_SAVED | source={source} | saved=0")
        return 0

    async with pool.acquire() as conn:
        for it in items:
            try:
                row = await conn.fetchrow(
                    _UPSERT_SQL,
                    it.source,
                    it.source_url,
                    it.title,
                    it.description,
                    it.starts_at.date() if it.starts_at else None,
                    it.venue,
                    normalize_city(it.city),
                    it.country,
                    _first_image(it.images),
                    it.dedup_key,
                    it.event_types or [],
                )
                if row is None:
                    unchanged += 1
                elif row['inserted']:
                    inserted += 1
                else:
                    updated += 1
            except Exception as e:
                logger.error(
                    f"EVENT_SAVE_ERROR | source={it.source} | "
                    f"url={it.source_url} | {e}"
                )

    saved = inserted + updated
    logger.info(
        f"EVENT_SAVED | source={source} | saved={saved} | "
        f"inserted={inserted} | updated={updated} | unchanged={unchanged}"
    )
    return saved
