from __future__ import annotations

import asyncio
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
from loguru import logger

from event_collector.scrapers.base import BaseScraper, EventItem
from event_collector.normalize import clean_html, parse_datetime, build_dedup_key
from collector.categorizer_v2 import categorize

BELGRADE_TZ = ZoneInfo('Europe/Belgrade')

API_BASE = "https://polako-prod-5qyaj.ondigitalocean.app/data/events"
ASSETS_BASE = "https://polako-prod-5qyaj.ondigitalocean.app/assets"
SITE_BASE = "https://polakohedonist.club/en/events"
TIMEOUT = 20
PAGE_SIZE = 500
PAGE_DELAY = 2.0   # пауза между страницами (как rateLimit в оригинале)
YEARS_AHEAD = 3


API_FIELDS = ",".join([
    "id", "name", "start_date", "status", "base_currency", "duration",
    "visit_price", "price_type", "for_adults", "book_link",
    "image_cover.id", "image_cover.filename_disk",
    "location.name", "location.address_text",
    "location.city.name", "location.city.country.code",
    "event_type.name",
    "translations.event_name", "translations.event_description",
    "translations.language",
    "translations.image_cover.id", "translations.image_cover.filename_disk",
    "description",
])

LANG_PRIORITY = ["ru", "en", "sr"]


def _asset_url(cover: dict | None) -> str | None:
    if not cover:
        return None
    cover_id = cover.get("id")
    if not cover_id:
        return None
    filename = cover.get("filename_disk")
    if filename:
        return f"{ASSETS_BASE}/{cover_id}/{filename}"
    return f"{ASSETS_BASE}/{cover_id}"


class PolakoScraper(BaseScraper):
    source = 'polako'

    def __init__(self, *args, **kwargs):
        pass

    def _build_filter(self) -> str:
        today = datetime.now(BELGRADE_TZ)
        year = today.year + YEARS_AHEAD
        frm = today.strftime('%Y-%m-%d') + " 00:00:00"
        to = f"{year}-{today.strftime('%m-%d')} 23:59:59"
        return json.dumps({
            "_and": [
                {"_or": [{"hidden": {"_eq": False}}, {"hidden": {"_null": True}}]},
            ],
            "status": {"_in": ["scheduled"]},
            "start_date": {"_between": [frm, to]},
        })

    async def fetch(self) -> list[EventItem]:
        logger.info("POLAKO_FETCH | start")
        api_filter = self._build_filter()

        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            try:
                r = await client.get(API_BASE, params={
                    "aggregate[count]": "id",
                    "filter": api_filter,
                })
                r.raise_for_status()
                total = int(r.json()["data"][0]["count"]["id"])
            except Exception as e:
                logger.error(f"POLAKO_COUNT_ERROR | {e}")
                return []

            logger.info(f"POLAKO_TOTAL | count={total}")
            if total == 0:
                return []

            items: list[EventItem] = []
            offset = 0
            page = 0
            while offset < total:
                page += 1
                try:
                    r = await client.get(API_BASE, params={
                        "fields": API_FIELDS,
                        "limit": str(PAGE_SIZE),
                        "offset": str(offset),
                        "sort": "start_date,id",
                        "filter": api_filter,
                    })
                    r.raise_for_status()
                    data = r.json()
                except Exception as e:
                    logger.error(f"POLAKO_PAGE_ERROR | offset={offset} | {e}")
                    break

                if data.get("errors"):
                    logger.error(f"POLAKO_API_ERROR | {data['errors'][0].get('message')}")
                    break

                events = data.get("data") or []
                if not events:
                    break

                for ev in events:
                    item = self._build_item(ev)
                    if item:
                        items.append(item)

                logger.debug(f"POLAKO_PAGE | page={page} | got={len(events)}")
                offset += len(events)

                if offset < total:
                    await asyncio.sleep(PAGE_DELAY)

        logger.info(f"POLAKO_DONE | total_events={len(items)}")
        return items

    def _build_item(self, ev: dict) -> EventItem | None:
        if not ev.get("id"):
            return None

        slug = ev.get("slug")
        if not slug:
            return None
        source_url = f"{SITE_BASE}/{slug}"

        tr = self._pick_translation(ev.get("translations"))
        title = (tr or {}).get("event_name") or ev.get("name")
        if not title:
            return None
        description = clean_html((tr or {}).get("event_description"))
        if not description:
            description = clean_html(ev.get("description"))

        starts_at = parse_datetime(ev.get("start_date"))
        if not starts_at:
            return None

        loc = ev.get("location") or {}
        city_obj = (loc.get("city") or {})
        country = ((city_obj.get("country") or {}).get("code"))
        if not country:
            return None

        venue = loc.get("name")
        city = city_obj.get("name")

        cat_text = title
        if description:
            cat_text = f"{title} {description}"
        event_types = categorize(cat_text, channel='polako', position=0)

        images = self._pick_cover(ev)

        return EventItem(
            source=self.source,
            source_url=source_url,
            title=title,
            description=description,
            starts_at=starts_at,
            venue=venue,
            city=city,
            country=country,
            event_types=event_types,
            images=images,
            dedup_key=build_dedup_key(title, starts_at, city),
        )

    def _pick_translation(self, translations) -> dict | None:
        if not translations:
            return None
        for lang in LANG_PRIORITY:
            for t in translations:
                if t.get("language") == lang:
                    return t
        return translations[0]

    def _pick_cover(self, ev: dict) -> str | None:

        root_url = _asset_url(ev.get("image_cover"))
        if root_url:
            return root_url

        translations = ev.get("translations") or []

        for lang in LANG_PRIORITY:
            for t in translations:
                if t.get("language") != lang:
                    continue
                url = _asset_url(t.get("image_cover"))
                if url:
                    return url

        for t in translations:
            url = _asset_url(t.get("image_cover"))
            if url:
                return url

        return None
