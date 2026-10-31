# event_collector/scrapers/ticrs.py
# tic.rs: листинг — серверный HTML, список событий — карточки
# <article class="event-card" data-event-id="…"> с датой (<time datetime>),
# местом, заголовком и картинкой. «Показать ещё» = следующая HTML-страница по
# data-next-url у элемента [data-tic-catalog-pages].
# Описание — из JSON API /api/event/{id} (best-effort: при сбое событие
# всё равно сохраняется по данным карточки).

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx
from loguru import logger
from selectolax.parser import HTMLParser

from event_collector.concurrency import gather_limited
from event_collector.scrapers.base import BaseScraper, EventItem
from event_collector.normalize import clean_html, build_dedup_key
from event_collector.retention import too_far
from collector.categorizer_v2 import categorize

BELGRADE_TZ = ZoneInfo('Europe/Belgrade')
SITE = "https://tic.rs"
LISTING = f"{SITE}/ru/belgrad"
DETAIL = f"{SITE}/api/event/{{id}}"
TIMEOUT = 20
PAGE_DELAY = 0.5
MAX_PAGES = 50   # предохранитель от зацикливания пагинации

HTML_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/html",
    "Accept-Language": "ru,en;q=0.8",
}
JSON_HEADERS = {**HTML_HEADERS, "Accept": "application/json"}

_CF_FORMAT_RE = re.compile(r"format=[a-z0-9]+", re.IGNORECASE)
_SRCSET_2X_RE = re.compile(r"(\S+)\s+2x")


def _force_jpeg(url: str | None) -> str | None:

    if not url:
        return url
    if "format=" in url.lower():
        return _CF_FORMAT_RE.sub("format=jpeg", url)
    if "imagedelivery.net" in url and re.search(r"/(?:w|h|fit)=", url):
        return url.rstrip("/") + ",format=jpeg"
    return url


class TicRsScraper(BaseScraper):
    source = 'ticrs'

    def __init__(self, *args, **kwargs):
        pass

    async def fetch(self, known_urls: set[str] | None = None) -> list[EventItem]:
        logger.info("TICRS_FETCH | start")
        # размер листинга для мониторинга (None = листинг не получен)
        self.listed_count = None
        known_urls = known_urls or set()
        now = datetime.now(timezone.utc)

        async with httpx.AsyncClient(timeout=TIMEOUT, headers=HTML_HEADERS,
                                     follow_redirects=True) as client:
            cards = await self._collect_cards(client)
            logger.info(f"TICRS_IDS | count={len(cards)}")
            self.listed_count = len(cards)

            # Известные и прошедшие отсекаем по данным карточки ДО detail-запроса.
            fresh = []
            skipped_known = skipped_past = 0
            for card in cards:
                if card["source_url"] in known_urls:
                    skipped_known += 1
                elif card["starts_at"] < now:
                    skipped_past += 1
                    logger.debug(
                        f"TICRS_PAST | {card['title']} | {card['starts_at'].date()}"
                    )
                elif too_far(card["starts_at"].date()):
                    logger.debug(
                        f"TICRS_TOO_FAR | {card['title']} | {card['starts_at'].date()}"
                    )
                else:
                    fresh.append(card)
            logger.info(
                f"TICRS_NEW | listed={len(cards)} | new={len(fresh)} | "
                f"skipped_known={skipped_known} | skipped_past={skipped_past}"
            )

            async def build(card):
                try:
                    return await self._build_item(client, card)
                except Exception as e:
                    logger.warning(f"TICRS_DETAIL_ERROR | id={card['id']} | {e}")
                    return None

            built = await gather_limited(fresh, build)
            items = [it for it in built if it]

        logger.info(f"TICRS_DONE | total_events={len(items)}")
        return items

    # ------------------------------------------------------------------
    # листинг

    async def _collect_cards(self, client) -> list[dict]:
        cards: list[dict] = []
        seen_ids: set[str] = set()
        seen_urls: set[str] = set()
        url: str | None = LISTING
        pages = 0
        while url and url not in seen_urls and pages < MAX_PAGES:
            seen_urls.add(url)
            try:
                r = await client.get(url)
                r.raise_for_status()
            except Exception as e:
                logger.warning(f"TICRS_LISTING_ERROR | url={url} | {e}")
                break
            pages += 1
            page_cards, next_url = self._parse_listing(r.text)
            if not page_cards:
                logger.warning(f"TICRS_LISTING_EMPTY | url={url}")
            for card in page_cards:
                if card["id"] not in seen_ids:
                    seen_ids.add(card["id"])
                    cards.append(card)
            if next_url:
                next_url = next_url if next_url.startswith("http") else SITE + next_url
                await asyncio.sleep(PAGE_DELAY)
            url = next_url or None
        logger.debug(f"TICRS_PAGES | pages={pages} | cards={len(cards)}")
        return cards

    def _parse_listing(self, html: str) -> tuple[list[dict], str | None]:
        tree = HTMLParser(html)
        cards = []
        for art in tree.css("article.event-card"):
            card = self._parse_card(art)
            if card:
                cards.append(card)
        panel = tree.css_first("[data-tic-catalog-pages]")
        next_url = (panel.attributes.get("data-next-url") or "").strip() if panel else ""
        return cards, next_url or None

    def _parse_card(self, art) -> dict | None:
        eid = art.attributes.get("data-event-id")
        link = art.css_first("h2.event-title a") or art.css_first("a[href]")
        time_node = art.css_first(".date-line time")
        if not eid or not link or not time_node:
            return None
        href = (link.attributes.get("href") or "").strip()
        title = link.text(strip=True)
        if not href or not title:
            return None
        starts_at = self._parse_iso(time_node.attributes.get("datetime"))
        if starts_at is None:
            return None
        venue_node = art.css_first(".event-place .venue")
        return {
            "id": eid,
            "title": title,
            "source_url": href if href.startswith("http") else SITE + href,
            "starts_at": starts_at,
            "venue": venue_node.text(strip=True) or None if venue_node else None,
            "image": self._card_image(art),
        }

    def _card_image(self, art) -> str | None:
        img = art.css_first("a.poster-link img") or art.css_first("img")
        if not img:
            return None
        srcset = img.attributes.get("srcset") or ""
        m = _SRCSET_2X_RE.search(srcset)
        return _force_jpeg(m.group(1) if m else img.attributes.get("src"))

    def _parse_iso(self, s: str | None) -> datetime | None:
        if not s:
            return None
        try:
            dt = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=BELGRADE_TZ)
        return dt

    # ------------------------------------------------------------------
    # detail (описание)

    async def _build_item(self, client, card: dict) -> EventItem:
        description = await self._fetch_description(client, card["id"])

        title = card["title"]
        cat_text = f"{title} {description}" if description else title
        event_types = categorize(cat_text, channel='ticrs', position=0)

        return EventItem(
            source=self.source,
            source_url=card["source_url"],
            title=title,
            description=description,
            starts_at=card["starts_at"],
            venue=card["venue"],
            city="Belgrade",
            country="RS",
            event_types=event_types,
            images=card["image"],
            dedup_key=build_dedup_key(title, card["starts_at"], "Belgrade"),
        )

    async def _fetch_description(self, client, eid: str) -> str | None:
        """Описание из /api/event/{id}. Сбой не критичен: None."""
        try:
            r = await client.get(DETAIL.format(id=eid), headers=JSON_HEADERS)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            d = r.json()
        except Exception as e:
            logger.debug(f"TICRS_DESC_ERROR | id={eid} | {e}")
            return None
        obj = d.get("data") if isinstance(d, dict) and "data" in d else d
        if not isinstance(obj, dict):
            return None
        return clean_html(obj.get("description") or obj.get("description_html"))
