from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx
from loguru import logger
from selectolax.parser import HTMLParser

from event_collector.scrapers.base import BaseScraper, EventItem
from event_collector.normalize import clean_html, build_dedup_key

BELGRADE_TZ = ZoneInfo('Europe/Belgrade')
HOME = "https://new.gigstix.com/"
TIMEOUT = 20
EVENT_DELAY = 0.3
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

CATEGORY_MAP = {
    "koncert":   "Концерты",
    "festival":  "Фестивали",
    "pozoriste": "Кино/Театр",
    "stand-up":  "Игры и развлечения",
    "sport":     "Спорт/активный отдых",
}

SERBIAN_MONTHS = {
    "januara": 1, "februara": 2, "marta": 3, "aprila": 4,
    "maja": 5, "juna": 6, "jula": 7, "avgusta": 8,
    "septembra": 9, "oktobra": 10, "novembra": 11, "decembra": 12,
}

_ICS_RE = re.compile(
    r"gt_event_ics\.addEvent\(\s*'[^']*',\s*'[^']*',\s*'[^']*',\s*'([^']*)'"
)
_ICS_DATE_RE = re.compile(
    r"^(\d{1,2})/(\d{1,2})/(\d{4})\s+(\d{1,2}):(\d{2})\s*(am|pm)$", re.I
)
_SR_DATE_RE = re.compile(
    r"(\d{1,2})\.\s+(\w+)\s+(\d{4})\.\s+(\d{1,2})\.(\d{2})"
)
_CAT_RE = re.compile(r"eventcat/([a-z0-9-]+)")
_PRICE_RE = re.compile(r"([\d.]+)\s*(DIN|RSD)", re.I)


class GigstixScraper(BaseScraper):
    source = 'gigstix'

    def __init__(self, *args, **kwargs):
        pass

    async def fetch(self) -> list[EventItem]:
        logger.info("GIGSTIX_FETCH | start")

        async with httpx.AsyncClient(timeout=TIMEOUT, headers=UA,
                                     follow_redirects=True) as client:
            event_urls = await self._collect_urls(client)
            logger.info(f"GIGSTIX_URLS | count={len(event_urls)}")

            items: list[EventItem] = []
            now = datetime.now(timezone.utc)
            for url in event_urls:
                try:
                    item = await self._fetch_event(client, url, now)
                    if item:
                        items.append(item)
                except Exception as e:
                    logger.warning(f"GIGSTIX_EVENT_ERROR | {url} | {e}")
                await asyncio.sleep(EVENT_DELAY)

        logger.info(f"GIGSTIX_DONE | total_events={len(items)}")
        return items

    async def _collect_urls(self, client) -> list[str]:
        r = await client.get(HOME)
        r.raise_for_status()
        tree = HTMLParser(r.text)
        seen: set[str] = set()
        ordered: list[str] = []
        for a in tree.css(".gt-event-listing .gt-title a[href]"):
            href = a.attributes.get("href", "")
            if href and "/event/" in href:
                href = href.replace("https://gigstix.com", "https://new.gigstix.com")
                if href not in seen:
                    seen.add(href)
                    ordered.append(href)
        return ordered

    async def _fetch_event(self, client, url: str, now: datetime) -> EventItem | None:
        r = await client.get(url)
        r.raise_for_status()
        html = r.text
        t = HTMLParser(html)

        title_node = t.css_first("head title")
        title = None
        if title_node:
            title = title_node.text().split(" - ")[0].strip() or None
        if not title:
            return None

        starts_at = self._parse_ics(html)
        if not starts_at:
            sr_node = t.css_first(".gt-start-date .gt-inner")
            if sr_node:
                starts_at = self._parse_serbian(sr_node.text(strip=True))
        if not starts_at:
            logger.debug(f"GIGSTIX_NO_DATE | {url}")
            return None
        if starts_at < now:
            logger.debug(f"GIGSTIX_PAST | {title} | {starts_at.date()}")
            return None

        description = self._extract_description(t)

        venue = self._first_text(t, ".gt-venue .gt-inner li")
        city = self._first_text(t, ".gt-locations .gt-inner li")

        og_image = t.css_first('meta[property="og:image"]')
        images = (og_image.attributes["content"]
                  if (og_image and og_image.attributes.get("content")) else None)

        event_types = self._resolve_categories(t)
        tickets, is_soldout = self._parse_tickets(t, url)

        return EventItem(
            source=self.source,
            source_url=url,
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

    def _first_text(self, tree, sel: str) -> str | None:
        n = tree.css_first(sel)
        return n.text(strip=True) or None if n else None

    def _extract_description(self, tree) -> str | None:
        node = (tree.css_first(".gt-page-content .gt-content") or
                tree.css_first('[data-gt-type="content"] .gt-content'))
        if not node:
            return None
        return clean_html(node.html)

    def _parse_ics(self, html: str) -> datetime | None:
        m = _ICS_RE.search(html)
        if not m:
            return None
        dm = _ICS_DATE_RE.match(m.group(1).strip())
        if not dm:
            return None
        month, day, year, hour, minute, ampm = dm.groups()
        hour = int(hour)
        if ampm.lower() == "pm" and hour != 12:
            hour += 12
        if ampm.lower() == "am" and hour == 12:
            hour = 0
        try:
            return datetime(int(year), int(month), int(day),
                            hour, int(minute), tzinfo=BELGRADE_TZ)
        except ValueError:
            return None

    def _parse_serbian(self, text: str) -> datetime | None:
        if not text:
            return None
        m = _SR_DATE_RE.search(text)
        if not m:
            return None
        day, month_name, year, hour, minute = m.groups()
        month = SERBIAN_MONTHS.get(month_name.lower())
        if not month:
            return None
        try:
            return datetime(int(year), month, int(day),
                            int(hour), int(minute), tzinfo=BELGRADE_TZ)
        except ValueError:
            return None

    def _resolve_categories(self, tree) -> list[str]:
        result: list[str] = []
        for a in tree.css(".gt-categories .gt-inner li a[href]"):
            href = a.attributes.get("href", "")
            m = _CAT_RE.search(href)
            if not m:
                continue
            cat = CATEGORY_MAP.get(m.group(1))
            if cat and cat not in result:
                result.append(cat)
        return result

    def _parse_tickets(self, tree, url: str):
        prices = []
        for el in tree.css(".gt-tickets-price"):
            text = el.text(strip=True)
            pm = _PRICE_RE.search(text)
            if pm:
                try:
                    prices.append(int(pm.group(1).replace(".", "")))
                except ValueError:
                    pass
        is_soldout = len(tree.css(".gt-tickets-price-sold-out")) > 0
        if not prices:
            return [{"url": url, "currency": "RSD"}], is_soldout
        return [{
            "url": url, "currency": "RSD",
            "price": prices[0], "prices": prices,
        }], is_soldout
