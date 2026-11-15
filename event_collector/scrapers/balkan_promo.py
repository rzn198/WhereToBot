"""balkan-promo.com — концерты Balkan Promo (с ноября 2026 сайт на собственной вёрстке).

Источник данных — главная страница: JSON в <script type="application/json" id="gigs"> со всеми концертами сезона
(название, дата ISO, клуб, адрес, картинка, ссылка на страницу концерта, билеты, признак отмены).
Страница концерта (/koncerty/<id>/) читается только ради описания — если не удалось, событие всё равно сохраняется.
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx
from loguru import logger
from selectolax.parser import HTMLParser

from event_collector.city_normalizer import find_city_in_text
from event_collector.normalize import build_dedup_key
from event_collector.scrapers.base import BaseScraper, EventItem

BELGRADE_TZ = ZoneInfo('Europe/Belgrade')
HOME = "https://balkan-promo.com/"
BASE = "https://balkan-promo.com"
TIMEOUT = 20
EVENT_DELAY = 0.3
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
START_HOUR = 20                      # время сайт не отдаёт — как и раньше, 20:00 (в БД хранится только дата)

FIXED_CATEGORY = ["Концерты"]

_SERVICE_MARKERS = (
    "билет приходит", "билеты придут", "скачайте билет", "криптовалют", "trc20", "без комиссии",
    "кнопка", "ticketscloud", "xtix", "ticketshop", "на почту", "tickets will be sent", "email address",
)
# Фраза описания всего сайта на главной и общих страницах — не описание концерта
_GENERIC_DESCRIPTIONS = ("рок, метал и панк в клубах",)


def _abs(url: str | None) -> str | None:
    if not url:
        return None
    return url if url.startswith("http") else f"{BASE}/{url.lstrip('/')}"


def parse_gigs(html: str) -> list[dict] | None:
    """Список концертов из JSON главной. None — блока нет или он не разобрался (вёрстка изменилась)."""
    node = HTMLParser(html).css_first('script#gigs')
    if node is None:
        return None
    try:
        data = json.loads(node.text(strip=False))
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, list) else None


def club_addresses(html: str) -> dict[str, str]:
    """Адрес клуба по его ссылке (/kluby/<id>/) из блока «Где проходят концерты»."""
    out: dict[str, str] = {}
    tree = HTMLParser(html)
    for block in tree.css('#kluby .club, #kluby .amb'):
        link = block.css_first('h3 a')
        addr = block.css_first('.addr')
        if link and addr:
            out[link.attributes.get('href', '')] = addr.text(strip=True)
    return out


def extract_description(html: str) -> str | None:
    """Описание концерта: блок «О концерте» (section.c-about) целиком, абзацы через пустую строку.
    og:description не берём: на сайте это «Название: дата, клуб, город. ...» обрезанное до ~160 символов (на полуслове).
    Если блока нет (у концерта нет описания) — None: лучше пустое описание, чем шаблонная строка из даты и клуба."""
    tree = HTMLParser(html)
    block = tree.css_first('section.c-about')
    if block is None:
        return None
    parts = []
    for p in block.css('p'):
        txt = p.text(strip=True)
        low = txt.lower()
        if len(txt) < 20 or any(mk in low for mk in _SERVICE_MARKERS) or any(g in low for g in _GENERIC_DESCRIPTIONS):
            continue
        parts.append(txt)
    return '\n\n'.join(parts) or None


class BalkanPromoScraper(BaseScraper):
    source = 'balkan-promo'

    def __init__(self, *args, **kwargs):
        pass

    async def fetch(self) -> list[EventItem]:
        logger.info("BALKAN_FETCH | start")
        self.listed_count = None

        async with httpx.AsyncClient(timeout=TIMEOUT, headers=UA, follow_redirects=True) as client:
            r = await client.get(HOME)
            r.raise_for_status()
            gigs = parse_gigs(r.text)
            if gigs is None:
                logger.warning("BALKAN_LISTING_CHANGED | блок #gigs не найден или не разобрался: вёрстка сайта изменилась")
                self.listed_count = 0
                return []
            addresses = club_addresses(r.text)
            self.listed_count = len(gigs)
            logger.info(f"BALKAN_URLS | count={len(gigs)}")

            today = datetime.now(BELGRADE_TZ).date()
            items: list[EventItem] = []
            for gig in gigs:
                try:
                    item = self._build_item(gig, addresses, today)
                    if item is None:
                        continue
                    item.description = await self._fetch_description(client, item.source_url)
                    items.append(item)
                except Exception as e:
                    logger.warning(f"BALKAN_EVENT_ERROR | {gig.get('u')} | {type(e).__name__}: {e}")
                await asyncio.sleep(EVENT_DELAY)

        logger.info(f"BALKAN_DONE | total_events={len(items)}")
        return items

    def _build_item(self, gig: dict, addresses: dict[str, str], today: date) -> EventItem | None:
        title = (gig.get('n') or '').strip()
        url = _abs(gig.get('u'))
        if not title or not url:
            return None
        if gig.get('s') == 'cancelled':
            logger.debug(f"BALKAN_CANCELLED | {title} | {gig.get('d')}")
            return None
        try:
            day = date.fromisoformat(gig.get('d') or '')
        except ValueError:
            logger.debug(f"BALKAN_NO_DATE | {title} | d={gig.get('d')!r}")
            return None
        if day < today:
            logger.debug(f"BALKAN_PAST | {title} | {day}")
            return None

        starts_at = datetime(day.year, day.month, day.day, START_HOUR, 0, tzinfo=BELGRADE_TZ)
        club_url = gig.get('cu') or ''
        city_text = f"{gig.get('where') or ''}, {addresses.get(club_url, '')}"
        city = find_city_in_text(city_text, default="Belgrade")

        return EventItem(
            source=self.source,
            source_url=url,
            title=title,
            starts_at=starts_at,
            venue=(gig.get('club') or '').strip() or None,
            city=city,
            country="RS",
            event_types=list(FIXED_CATEGORY),
            category_basis='fixed',
            images=_abs(gig.get('img')),
            dedup_key=build_dedup_key(title, starts_at, city),
        )

    async def _fetch_description(self, client, url: str) -> str | None:
        try:
            r = await client.get(url)
            r.raise_for_status()
            return extract_description(r.text)
        except Exception as e:
            logger.debug(f"BALKAN_DESC_SKIP | {url} | {type(e).__name__}: {e}")
            return None
