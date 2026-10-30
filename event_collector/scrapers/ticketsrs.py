# event_collector/scrapers/ticketsrs.py
# Порт Node (plugins/ticketsrs) на Python.
# POST API /web/api/data/agnosticget с SessionCode.
# Листинг самодостаточен (Venue/Price/DateTime/StartDate есть) -> без detail.
# Обход 21 категории с пагинацией. Категория из slug -> русская (подход B).
#
# ВНИМАНИЕ: SessionCode захардкожен. Если API начнёт отдавать пусто/ошибку —
# сессия протухла, нужно обновить SESSION_CODE (лог TICKETSRS_SESSION_WARN).

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx
from loguru import logger

from event_collector.scrapers.base import BaseScraper, EventItem
from event_collector.normalize import build_dedup_key, clean_html

BELGRADE_TZ = ZoneInfo('Europe/Belgrade')
API = "https://tickets.rs/web/api/data/agnosticget"
SITE = "https://tickets.rs"
SESSION_CODE = "12C1E0F33673E440A77C47D316944EB8"
TIMEOUT = 20
PAGE_DELAY = 0.5
CAT_DELAY = 1.0
DESC_DELAY = 0.2
UA = {"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"}

# Категоризация ticketsrs — гибрид: уточнения + агрегаты.
# REFINE_MAP: специфичные подкатегории, обходятся ПЕРВЫМИ — дают точную
#   категорию для своих событий (иначе агрегат огрубил бы: стендап->Театр).
# AGGREGATE_MAP: слаги-агрегаты "Svi događaji" разделов, обходятся ПОСЛЕ —
#   добирают все остальные события раздела (грубая категория).
# Событие из уточнения уже в seen_urls -> агрегат его пропустит (точное победило).
REFINE_MAP = {
    "decije_predstave_6":  "Семейные/детские",
    "standup_42":          "Игры и развлечения",
    "sou_26":              "Игры и развлечения",
    "tura_sa_vodicem_25":  "Поездки и экскурсии",
    "putovanje_27":        "Поездки и экскурсии",
}

AGGREGATE_MAP = {
    "koncerti_muzika":          "Концерты",
    "pozorista":                "Кино/Театр",
    "sport":                    "Спорт/активный отдых",
    "festivali":                "Фестивали",
    "muzeji_izlozba":           "Выставки/Ярмарки",
    "konferencija_seminar_33":  "Мастер-классы/Нетворкинг",
    "filmska_projekcija_41":    "Кино/Театр",
}

# Итоговый порядок обхода: сначала уточнения, потом агрегаты.
CATEGORY_MAP = {**REFINE_MAP, **AGGREGATE_MAP}

_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})")
_ID_RE = re.compile(r"_(\d+)$")


class TicketsRsScraper(BaseScraper):
    source = 'ticketsrs'

    def __init__(self, *args, **kwargs):
        pass

    async def fetch(self, known_urls: set[str] | None = None) -> list[EventItem]:
        logger.info("TICKETSRS_FETCH | start")
        now = datetime.now(timezone.utc)
        items: list[EventItem] = []
        seen_urls: set[str] = set()
        known_urls = known_urls or set()

        async with httpx.AsyncClient(timeout=TIMEOUT, headers=UA) as client:
            for slug, category in CATEGORY_MAP.items():
                try:
                    cat_items = await self._fetch_category(
                        client, slug, category, now, seen_urls)
                    items.extend(cat_items)
                except Exception as e:
                    logger.warning(f"TICKETSRS_CAT_ERROR | slug={slug} | {e}")
                await asyncio.sleep(CAT_DELAY)

            # Отсекаем уже известные события ДО дорогой фазы 2 (2 detail-запроса
            # на событие). Новые detail делаем только для новых.
            total_listed = len(items)
            items = [it for it in items if it.source_url not in known_urls]
            logger.info(
                f"TICKETSRS_NEW | listed={total_listed} | "
                f"new={len(items)} | skipped_known={total_listed - len(items)}"
            )

            # Фаза 2: детали для каждого события — описание (PageConfig)
            # и место/гео (Get4Event). Два запроса на событие.
            logger.info(f"TICKETSRS_DETAIL | enriching {len(items)} events")
            for item in items:
                # описание
                try:
                    desc = await self._fetch_description(client, item.source_url)
                    if desc:
                        item.description = desc
                except Exception as e:
                    logger.debug(f"TICKETSRS_DESC_ERROR | {item.source_url} | {e}")
                # город / полное имя площадки
                try:
                    venue, town = await self._fetch_venue(client, item.source_url)
                    if town:
                        item.city = town
                    if venue:
                        item.venue = venue   # полное имя из Get4Event лучше листингового
                except Exception as e:
                    logger.debug(f"TICKETSRS_VENUE_ERROR | {item.source_url} | {e}")
                await asyncio.sleep(DESC_DELAY)

        logger.info(f"TICKETSRS_DONE | total_events={len(items)}")
        return items

    async def _fetch_venue(self, client, source_url: str):
        """web_Get4Event -> (venue_name, town)."""
        slug = source_url.rstrip("/").split("/")[-1]
        payload = {
            "JSONParams": {
                "UserName": "tickets.rs", "SessionCode": SESSION_CODE,
                "UserType": "2", "Lang": "SR", "Slug": slug,
                "LayoutType": "event", "WidgetId": 27,
                "AKCIJA": "GET_EVENT_WIDGET",
            },
            "sproc": "web_Get4Event",
        }
        r = await client.post(API, json=payload)
        r.raise_for_status()
        d = r.json()
        data = d.get("data")
        if not isinstance(data, dict):
            return None, None
        venue = data.get("Venue") or {}
        if not isinstance(venue, dict):
            return None, None
        name = venue.get("Name") or None
        town = venue.get("Town") or None
        return name, town

    async def _fetch_description(self, client, source_url: str) -> str | None:
        """web__Get_PageConfig -> HTML-описание из Layout (устойчивый поиск)."""
        # source_url = 'https://tickets.rs/event/mira_skoric_24498'
        # slug для PageConfig = 'mira_skoric_24498' (без 'event/')
        slug = source_url.rstrip("/").split("/")[-1]
        payload = {
            "JSONParams": {
                "UserName": "tickets.rs", "SessionCode": SESSION_CODE,
                "UserType": "2", "Lang": "SR", "Slug": slug,
                "LayoutType": "event",
            },
            "sproc": "web__Get_PageConfig",
        }
        r = await client.post(API, json=payload)
        r.raise_for_status()
        d = r.json()
        data = d.get("data")
        if not isinstance(data, dict):
            return None
        # рекурсивно ищем самый длинный Text-виджет с HTML (описание)
        html = self._find_description_text(data.get("Layout"))
        if html:
            return clean_html(html)
        return None

    def _find_description_text(self, node) -> str | None:
        """Ищет в Layout самый длинный строковый Text (HTML-описание).
        Устойчиво к структуре табов/колонок (не по фиксированным индексам)."""
        best = None
        best_len = 0

        def walk(obj):
            nonlocal best, best_len
            if isinstance(obj, dict):
                for k, v in obj.items():
                    # виджет описания: ключ 'Text' со строкой-HTML
                    if k == "Text" and isinstance(v, str):
                        if ("<p" in v or "<div" in v or len(v) > 100) and len(v) > best_len:
                            best, best_len = v, len(v)
                    else:
                        walk(v)
            elif isinstance(obj, list):
                for v in obj:
                    walk(v)

        walk(node)
        return best

    async def _fetch_category(self, client, slug, category, now, seen_urls):
        first = await self._post(client, slug, 1)
        data = (first or {}).get("data")
        if not data:
            logger.warning(f"TICKETSRS_SESSION_WARN | slug={slug} | "
                           f"пустой ответ — возможно сессия протухла")
            return []

        total = data.get("TotalItems", 0)
        per_page = data.get("ItemsPerPage", 20) or 20
        total_pages = (total + per_page - 1) // per_page

        result = []
        # страница 1 уже загружена
        result.extend(self._parse_events(data.get("Events") or [],
                                         category, now, seen_urls))
        # остальные страницы
        for page in range(2, total_pages + 1):
            await asyncio.sleep(PAGE_DELAY)
            d = await self._post(client, slug, page)
            evs = ((d or {}).get("data") or {}).get("Events") or []
            result.extend(self._parse_events(evs, category, now, seen_urls))

        logger.debug(f"TICKETSRS_CAT | slug={slug} | pages={total_pages} | "
                     f"events={len(result)}")
        return result

    async def _post(self, client, slug, page):
        """POST листинга. Пробует голый slug, при пустом ответе — с category/."""
        for slug_variant in (slug, f"category/{slug}"):
            payload = {
                "JSONParams": {
                    "UserName": "tickets.rs", "SessionCode": SESSION_CODE,
                    "UserType": "2", "Lang": "EN", "Slug": slug_variant,
                    "LayoutType": "category", "Search": {}, "WidgetID": 2,
                    "CurrentPage": page,
                },
                "sproc": "web__Get_EventList",
            }
            r = await client.post(API, json=payload)
            r.raise_for_status()
            d = r.json()
            data = d.get("data") or {}
            if data.get("Events") or (data.get("TotalItems") or 0) > 0:
                return d
        # оба варианта пусты — возвращаем последний ответ (для логики пагинации)
        return d

    def _parse_events(self, events, category, now, seen_urls):
        result = []
        for ev in events:
            item = self._build_item(ev, category, now)
            if item and item.source_url not in seen_urls:
                seen_urls.add(item.source_url)
                result.append(item)
        return result

    def _build_item(self, ev, category, now) -> EventItem | None:
        title = (ev.get("Title") or "").strip()
        if not title:
            return None
        # фильтр ваучеров (как в Node)
        if "vaučer" in title.lower() or "vaucer" in title.lower():
            return None

        slug = ev.get("Slug") or ""   # 'event/evoken_25749'
        if not slug:
            return None
        source_url = f"{SITE}/{slug}"

        starts_at = self._parse_dt(ev.get("StartDate"), ev.get("DateTime"))
        if not starts_at:
            return None
        if starts_at < now:
            logger.debug(f"TICKETSRS_PAST | {title} | {starts_at.date()}")
            return None

        ends_at = self._parse_dt(ev.get("EndDate"), ev.get("DateTime"))

        venue = ev.get("Venue") or None
        price = ev.get("Price")
        tickets = [{
            "price": price, "currency": "RSD", "url": source_url,
        }] if price else [{"url": source_url, "currency": "RSD"}]

        images = [ev["ImgSrc"]] if ev.get("ImgSrc") else []

        return EventItem(
            source=self.source,
            source_url=source_url,
            title=title,
            starts_at=starts_at,
            venue=venue,
            country="RS",
            event_types=[category],
            images=images,
            dedup_key=build_dedup_key(title, starts_at, None),
        )

    def _parse_dt(self, iso_date, datetime_str) -> datetime | None:
        """StartDate ISO ('2026-08-12') + время из DateTime ('...19:00')."""
        if not iso_date:
            return None
        try:
            y, m, d = map(int, iso_date.split("-"))
        except (ValueError, AttributeError):
            return None
        hour, minute = 0, 0
        if datetime_str:
            tm = _TIME_RE.search(datetime_str)
            if tm:
                hour, minute = int(tm.group(1)), int(tm.group(2))
        try:
            return datetime(y, m, d, hour, minute, tzinfo=BELGRADE_TZ)
        except ValueError:
            return None
