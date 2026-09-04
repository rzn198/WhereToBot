# event_collector/scrapers/balkan_promo.py
# Порт Node (plugins/balkan-promo) на Python.
# HTML Flexbe CMS, два прохода:
#   Root:  homepage -> .flexbe-card -> URL событий
#   Event: страница -> таблица расписания [role=row]/[role=cell]
# Категория: всегда ['Концерты'] (balkan-promo только концерты).
# Дата: "11 Сентября"/"25 April" (RU+EN месяцы), без года -> тек./след. год.

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx
from loguru import logger
from selectolax.parser import HTMLParser

from event_collector.scrapers.base import BaseScraper, EventItem
from event_collector.normalize import build_dedup_key

BELGRADE_TZ = ZoneInfo('Europe/Belgrade')
HOME = "https://balkan-promo.com/"
BASE = "https://balkan-promo.com"
TIMEOUT = 20
EVENT_DELAY = 0.3
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

# balkan-promo — только концерты
FIXED_CATEGORY = ["Концерты"]

# Маркеры служебного текста (не описание) — инструкции про билеты/одежду/крипту
_SERVICE_MARKERS = (
    "билеты придут", "скачать сразу после оплаты", "подберите одежду",
    "минимальное количество ценных", "минимум ценных вещей",
    "боитесь простудиться", "только самое нужное",
    "криптовалют", "trc20", "без комиссии при покупке",
    "tickets without commission", "tickets will be sent",
    "email address you provided", "@ti", "на почту", "purchase tickets",
)

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}

_DATE_RE = re.compile(r"(\d{1,2})\s+(\S+)")
_BR_SPLIT = re.compile(r"<br\s*/?>", re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_CUR_RE = re.compile(r"\b(RSD|EUR|RUB|DIN)\b", re.I)


class BalkanPromoScraper(BaseScraper):
    source = 'balkan-promo'

    def __init__(self, *args, **kwargs):
        pass

    async def fetch(self) -> list[EventItem]:
        logger.info("BALKAN_FETCH | start")

        async with httpx.AsyncClient(timeout=TIMEOUT, headers=UA,
                                     follow_redirects=True) as client:
            urls = await self._collect_urls(client)
            logger.info(f"BALKAN_URLS | count={len(urls)}")

            items: list[EventItem] = []
            now = datetime.now(timezone.utc)
            for url in urls:
                try:
                    item = await self._fetch_event(client, url, now)
                    if item:
                        items.append(item)
                except Exception as e:
                    logger.warning(f"BALKAN_EVENT_ERROR | {url} | {e}")
                await asyncio.sleep(EVENT_DELAY)

        logger.info(f"BALKAN_DONE | total_events={len(items)}")
        return items

    async def _collect_urls(self, client) -> list[str]:
        r = await client.get(HOME)
        r.raise_for_status()
        tree = HTMLParser(r.text)
        seen: set[str] = set()
        ordered: list[str] = []
        for card in tree.css(".flexbe-card"):
            link = card.css_first(".component-button a")
            if not link:
                continue
            href = link.attributes.get("href", "")
            if not href or "contacts" in href.lower():
                continue
            url = href if href.startswith("http") else f"{BASE}/{href.lstrip('/')}"
            if url not in seen:
                seen.add(url)
                ordered.append(url)
        return ordered

    async def _fetch_event(self, client, url: str, now: datetime) -> EventItem | None:
        r = await client.get(url)
        r.raise_for_status()
        t = HTMLParser(r.text)

        # title из <title>: два шаблона
        #   RU: "Афиша выступлений X на Балканах" -> "X"
        #   EN: "Poster of X performances in the Balkans" -> "X"
        tt = t.css_first("head title")
        title = None
        if tt:
            raw = tt.text(strip=True)
            # русский шаблон
            title = re.sub(r"^Афиша выступлений\s+", "", raw)
            title = re.sub(r"\s+на Балканах$", "", title)
            # английский шаблон
            title = re.sub(r"^Poster of\s+", "", title, flags=re.I)
            title = re.sub(r"\s+performances?\s+in\s+the\s+Balkans$", "", title, flags=re.I)
            title = title.strip() or None
        if not title:
            return None

        date_str, venue, city, tickets = self._parse_table(t, url)
        starts_at = self._parse_date(date_str, now)
        if not starts_at:
            logger.debug(f"BALKAN_NO_DATE | {title} | date_str={date_str!r}")
            return None
        if starts_at < now:
            logger.debug(f"BALKAN_PAST | {title} | {starts_at.date()}")
            return None

        images = self._extract_image(t, url)
        description = self._extract_description(t)

        return EventItem(
            source=self.source,
            source_url=url,
            title=title,
            description=description,
            starts_at=starts_at,
            venue=venue,
            city=city,
            country="RS",
            event_types=list(FIXED_CATEGORY),
            images=images,
            dedup_key=build_dedup_key(title, starts_at, city),
        )

    def _parse_table(self, tree, url: str):
        date_str = venue = city = None
        tickets = []
        row = tree.css_first('[role="row"]')
        if not row:
            return date_str, venue, city, tickets
        cells = row.css('[role="cell"]')
        for i, cell in enumerate(cells):
            if i == 0:
                date_str = cell.text(strip=True)
            elif i == 1:
                et = cell.css_first(".element-text")
                cell_html = (et.html if et else cell.html) or ""
                parts = [_TAG_RE.sub("", p).strip()
                         for p in _BR_SPLIT.split(cell_html)]
                parts = [p for p in parts if p]
                if len(parts) >= 2:
                    city, venue = parts[0], parts[1]
                elif len(parts) == 1:
                    venue = parts[0]
            else:
                link_node = cell.css_first("a")
                link = link_node.attributes.get("href") if link_node else None
                btn = cell.css_first(".btn-text")
                btn_text = (btn.text(strip=True) if btn else cell.text(strip=True)) or ""
                if "crypto" in btn_text.lower():
                    continue
                cm = _CUR_RE.search(btn_text)
                currency = cm.group(1).upper() if cm else None
                if currency == "DIN":
                    currency = "RSD"
                if currency and link:
                    tickets.append({
                        "type": currency, "currency": currency, "url": link,
                    })
        if not tickets:
            tickets = [{"url": url, "currency": "RSD"}]
        return date_str, venue, city, tickets

    def _parse_date(self, date_str: str | None, now: datetime) -> datetime | None:
        if not date_str:
            return None
        m = _DATE_RE.search(date_str)
        if not m:
            return None
        day = int(m.group(1))
        month = MONTHS.get(m.group(2).lower())
        if not month:
            logger.warning(f"BALKAN_UNKNOWN_MONTH | {m.group(2)!r}")
            return None
        year = now.astimezone(BELGRADE_TZ).year
        try:
            dt = datetime(year, month, day, 20, 0, tzinfo=BELGRADE_TZ)
        except ValueError:
            return None
        # без года: если дата уже прошла — следующий год
        if dt < now:
            try:
                dt = datetime(year + 1, month, day, 20, 0, tzinfo=BELGRADE_TZ)
            except ValueError:
                return None
        return dt

    def _extract_description(self, tree) -> str | None:
        """
        Первый содержательный текстовый блок, не являющийся служебным
        (инструкции про билеты/одежду/крипту).
        """
        for n in tree.css('.element-text, [class*="text-holder"]'):
            txt = n.text(strip=True)
            if len(txt) < 80:
                continue
            low = txt.lower()
            if any(marker in low for marker in _SERVICE_MARKERS):
                continue
            return txt
        return None

    def _extract_image(self, tree, url: str) -> list[str]:
        bg = tree.css_first('div.component-bg[data-img-id]')
        if bg:
            img_id = bg.attributes.get("data-img-id")
            img_ext = bg.attributes.get("data-img-ext")
            if img_id and img_ext:
                return [f"{BASE}/img/{img_id}.{img_ext}"]
        og = tree.css_first('meta[property="og:image"]')
        if og and og.attributes.get("content"):
            c = og.attributes["content"]
            return [c if c.startswith("http") else f"{BASE}{c}"]
        return []
