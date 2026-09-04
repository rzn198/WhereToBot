# event_collector/scrapers/ticrs.py
# Порт Node (plugins/ticrs) на Python. Только Belgrade.
# Двухфазный:
#   Фаза 1: tic.rs/en/belgrade?page=N (XHR) -> JSON {data, pagination} -> id
#   Фаза 2: tic.rs/api/event/{id} -> русские name/description, geo, place
# Категория: categorizer_v2 по русскому title+description (как polako).
# ticrs — единственный источник с координатами (geo.lat/lon).

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx
from loguru import logger

from event_collector.scrapers.base import BaseScraper, EventItem
from event_collector.normalize import clean_html, build_dedup_key
from collector.categorizer_v2 import categorize

BELGRADE_TZ = ZoneInfo('Europe/Belgrade')
CITY = "belgrade"
# ВАЖНО: /ru/belgrade делает 301-редирект на /ru/belgrad (без 'e'),
# и при редиректе теряется ?page=N — пагинация ломается (всегда стр.1).
# Ходим сразу на конечный /ru/belgrad, тогда page сохраняется.
LISTING = "https://tic.rs/ru/belgrad"
DETAIL = "https://tic.rs/api/event/{id}"
TIMEOUT = 20
PAGE_DELAY = 0.5
DETAIL_DELAY = 0.2

XHR = {
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
}

# русские месяцы для date_text ("13 августа, 17:30")
MONTHS_RU = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}
MONTHS_EN = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}

_DATE_RE = re.compile(r"(\d{1,2})\s+(\w+),?\s+(\d{1,2}):(\d{2})")


class TicRsScraper(BaseScraper):
    source = 'ticrs'

    def __init__(self, *args, **kwargs):
        pass

    async def fetch(self) -> list[EventItem]:
        logger.info("TICRS_FETCH | start")
        now = datetime.now(timezone.utc)

        async with httpx.AsyncClient(timeout=TIMEOUT, headers=XHR,
                                     follow_redirects=True) as client:
            # Фаза 1: собираем id из листинга Belgrade
            event_ids = await self._collect_ids(client)
            logger.info(f"TICRS_IDS | count={len(event_ids)}")

            # Фаза 2: detail по каждому
            items: list[EventItem] = []
            for eid in event_ids:
                try:
                    item = await self._fetch_detail(client, eid, now)
                    if item:
                        items.append(item)
                except Exception as e:
                    logger.warning(f"TICRS_DETAIL_ERROR | id={eid} | {e}")
                await asyncio.sleep(DETAIL_DELAY)

        logger.info(f"TICRS_DONE | total_events={len(items)}")
        return items

    async def _collect_ids(self, client) -> list[str]:
        ids: list[str] = []
        seen: set[str] = set()
        page = 1
        while True:
            try:
                r = await client.get(LISTING, params={"page": str(page)})
                r.raise_for_status()
                d = r.json()
            except Exception as e:
                logger.warning(f"TICRS_LISTING_ERROR | page={page} | {e}")
                break

            data = d.get("data") or []
            for ev in data:
                eid = ev.get("id")
                if eid and str(eid) not in seen:
                    seen.add(str(eid))
                    ids.append(str(eid))

            pag = d.get("pagination") or {}
            total_pages = pag.get("total_pages") or 1
            current = pag.get("current_page")
            # защита: если API сбросил на стр.1 при запросе page>1 (редирект
            # съел параметр) — прекращаем, чтобы не собирать одну страницу вечно.
            if current is not None and page > 1 and current != page:
                logger.warning(
                    f"TICRS_PAGE_RESET | запросили page={page}, "
                    f"вернулась current_page={current} — стоп пагинации"
                )
                break
            if page >= total_pages or not data:
                break
            page += 1
            await asyncio.sleep(PAGE_DELAY)
        return ids

    async def _fetch_detail(self, client, eid: str, now: datetime) -> EventItem | None:
        r = await client.get(DETAIL.format(id=eid))
        if r.status_code == 404:
            return None
        r.raise_for_status()
        d = r.json()
        obj = d.get("data") if isinstance(d, dict) and "data" in d else d
        if not isinstance(obj, dict):
            return None

        # русские name/description из detail
        title = (obj.get("name") or "").strip()
        if not title:
            return None
        description = clean_html(obj.get("description") or obj.get("description_html"))

        starts_at = self._parse_date(obj.get("date_text"), obj.get("calendar"), now)
        if not starts_at:
            logger.debug(f"TICRS_NO_DATE | id={eid} | {title}")
            return None
        if starts_at < now:
            logger.debug(f"TICRS_PAST | {title} | {starts_at.date()}")
            return None

        # venue из place
        venue = self._parse_place(obj)

        # категоризация: русский title + description через categorizer_v2
        cat_text = title
        if description:
            cat_text = f"{title} {description}"
        event_types = categorize(cat_text, channel='ticrs', position=0)

        images = self._parse_images(obj.get("poster_urls"))
        tickets = self._parse_price(obj.get("formatted_price"), obj.get("url"))
        source_url = obj.get("url") or f"https://tic.rs/ru/belgrade/events/{eid}"

        return EventItem(
            source=self.source,
            source_url=source_url,
            title=title,
            description=description,
            starts_at=starts_at,
            venue=venue,
            city="Belgrade",
            country="RS",
            event_types=event_types,
            images=images,
            dedup_key=build_dedup_key(title, starts_at, "Belgrade"),
        )

    def _parse_date(self, date_text, calendar, now) -> datetime | None:
        # приоритет: calendar (ISO с годом), фолбэк date_text ("13 августа, 17:30")
        if calendar:
            iso = None
            # реальная структура: [{'date_start': '2026-09-07T20:00:00+02:00', ...}]
            if isinstance(calendar, list) and calendar:
                first = calendar[0]
                if isinstance(first, dict):
                    iso = (first.get("date_start") or first.get("start")
                           or first.get("date"))
            elif isinstance(calendar, dict):
                iso = (calendar.get("date_start") or calendar.get("start")
                       or calendar.get("date"))
            elif isinstance(calendar, str):
                iso = calendar
            if iso:
                dt = self._parse_iso(iso)
                if dt:
                    return dt

        if not date_text:
            return None
        m = _DATE_RE.search(date_text)
        if not m:
            return None
        day, month_name, hour, minute = m.groups()
        month = MONTHS_RU.get(month_name.lower()) or MONTHS_EN.get(month_name.lower())
        if not month:
            logger.warning(f"TICRS_UNKNOWN_MONTH | {month_name!r}")
            return None
        year = now.astimezone(BELGRADE_TZ).year
        try:
            dt = datetime(year, month, int(day), int(hour), int(minute), tzinfo=BELGRADE_TZ)
        except ValueError:
            return None
        # без года: если прошло — следующий год
        if dt < now:
            try:
                dt = datetime(year + 1, month, int(day), int(hour), int(minute), tzinfo=BELGRADE_TZ)
            except ValueError:
                return None
        return dt

    def _parse_iso(self, s: str) -> datetime | None:
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=BELGRADE_TZ)
            return dt
        except (ValueError, AttributeError):
            return None

    def _parse_place(self, obj) -> str | None:
        place = obj.get("place")
        if isinstance(place, dict):
            return place.get("name") or place.get("title")
        elif isinstance(place, str):
            return place
        return None

    def _parse_images(self, poster_urls) -> list[str]:
        if isinstance(poster_urls, dict):
            for key in ("2x", "1x", "original"):
                if poster_urls.get(key):
                    return [poster_urls[key]]
        elif isinstance(poster_urls, str):
            return [poster_urls]
        return []

    def _parse_price(self, formatted_price, url) -> list[dict]:
        ticket = {"url": url or "https://tic.rs", "currency": "RSD"}
        if formatted_price:
            m = re.search(r"([\d.]+)", formatted_price.replace(".", "").replace(",", "."))
            pm = re.search(r"[\d.,]+", formatted_price)
            if pm:
                num = pm.group().replace(".", "").replace(",", ".")
                try:
                    ticket["price"] = int(float(num))
                except ValueError:
                    pass
        return [ticket]
