from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone

import httpx
from loguru import logger

from event_collector.scrapers.base import BaseScraper, EventItem
from event_collector.normalize import parse_datetime, draftjs_to_text, build_dedup_key
from event_collector.city_normalizer import find_city_in_text

LISTING_URL = "https://bilet.rs/events"
DETAIL_URL = "https://app.bilet.rs/api/v1/public/shows/{id}"
TIMEOUT = 20
DETAIL_DELAY = 0.15
TAG_DELAY = 1.0

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


TAGS = ["Koncerti", "Festivali", "Izložbe", "Žurke",
        "Edukacije", "Kursevi", "Seminari", "Webinari"]

TAG_TO_CATEGORY = {
    "Koncerti":  "Концерты",
    "Festivali": "Фестивали",
    "Izložbe":   "Выставки/Ярмарки",
    "Žurke":     "Концерты",
    "Edukacije": "Мастер-классы/Нетворкинг",
    "Kursevi":   "Мастер-классы/Нетворкинг",
    "Seminari":  "Мастер-классы/Нетворкинг",
    "Webinari":  "Мастер-классы/Нетворкинг",
}

_JSONLD_RE = re.compile(
    r'<script type="application/ld\+json">(.*?)</script>', re.S
)
_ID_RE = re.compile(r'/events/(\d+)/')


class BiletRsScraper(BaseScraper):
    source = 'biletrs'

    def __init__(self, *args, **kwargs):
        self._url_by_id: dict[str, str] = {}

    async def fetch(self) -> list[EventItem]:
        logger.info("BILETRS_FETCH | start")

        async with httpx.AsyncClient(timeout=TIMEOUT, headers=UA) as client:
            event_ids = await self._collect_ids(client)
            logger.info(f"BILETRS_IDS | unique={len(event_ids)}")

            items: list[EventItem] = []
            now = datetime.now(timezone.utc)
            for eid in event_ids:
                try:
                    item = await self._fetch_detail(client, eid, now)
                    if item:
                        items.append(item)
                except Exception as e:
                    logger.warning(f"BILETRS_DETAIL_ERROR | id={eid} | {e}")
                await asyncio.sleep(DETAIL_DELAY)

        logger.info(f"BILETRS_DONE | total_events={len(items)}")
        return items

    async def _collect_ids(self, client) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []
        for tag in TAGS:
            try:
                r = await client.get(LISTING_URL, params={"size": "10000", "tag": tag})
                r.raise_for_status()
            except Exception as e:
                logger.warning(f"BILETRS_LISTING_ERROR | tag={tag} | {e}")
                continue

            m = _JSONLD_RE.search(r.text)
            if not m:
                logger.debug(f"BILETRS_NO_JSONLD | tag={tag}")
                continue
            try:
                ld = json.loads(m.group(1))
            except Exception:
                continue

            items = ld.get("itemListElement") or []
            cnt = 0
            for it in items:
                item = it.get("item") or {}
                url = item.get("url") or it.get("url")
                if not url:
                    continue
                idm = _ID_RE.search(url)
                if not idm:
                    continue
                eid = idm.group(1)
                if eid not in seen:
                    seen.add(eid)
                    ordered.append(eid)
                    public_url = url.replace("https://app.bilet.rs", "https://bilet.rs")
                    self._url_by_id[eid] = public_url
                    cnt += 1
            logger.debug(f"BILETRS_TAG | tag={tag} | new_ids={cnt}")
            await asyncio.sleep(TAG_DELAY)
        return ordered

    async def _fetch_detail(self, client, eid: str, now: datetime) -> EventItem | None:
        r = await client.get(DETAIL_URL.format(id=eid))
        if r.status_code == 404:
            return None
        r.raise_for_status()
        d = r.json()

        title = (d.get("showTitle") or "").strip()
        if not title:
            return None

        starts_at = parse_datetime(d.get("showDate"))
        if not starts_at:
            return None
        if starts_at < now:
            logger.debug(f"BILETRS_PAST | id={eid} | {title} | {starts_at.date()}")
            return None

        ends_at = parse_datetime(d.get("endDate"))
        description = draftjs_to_text(d.get("description"))
        venue = d.get("manualLocation")
        city = find_city_in_text(venue, default="Belgrade")
        currency = d.get("currency") or "RSD"

        event_types = self._map_tags(d.get("tags"))

        images = d.get("eventImage") or None   # одна ссылка или None

        tickets = self._build_tickets(d.get("ticketTypes"), currency, eid)
        is_soldout = (d.get("status") == "COMPLETED")

        source_url = self._url_by_id.get(eid, f"https://bilet.rs/events/{eid}")

        return EventItem(
            source=self.source,
            source_url=source_url,
            title=title,
            description=description,
            starts_at=starts_at,
            venue=venue,
            city=city,
            country="RS",
            event_types=event_types,
            images=images,
            dedup_key=build_dedup_key(title, starts_at, city),
        )

    def _map_tags(self, tags) -> list[str]:
        if not tags or not isinstance(tags, list):
            return []
        result: list[str] = []
        for t in tags:
            text = (t or {}).get("text")
            cat = TAG_TO_CATEGORY.get(text)
            if cat and cat not in result:
                result.append(cat)
        return result

    def _build_tickets(self, ticket_types, currency: str, eid: str) -> list[dict]:
        source_url = self._url_by_id.get(eid, f"https://bilet.rs/events/{eid}")
        if not ticket_types or not isinstance(ticket_types, list):
            return [{"url": source_url, "currency": currency}]
        tickets = []
        for t in ticket_types:
            price = t.get("ticketPrice")
            tickets.append({
                "name": t.get("title"),
                "price": price,
                "currency": t.get("currency") or currency,
                "url": source_url,
            })
        return tickets or [{"url": source_url, "currency": currency}]
