from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone

import httpx
from loguru import logger

from event_collector.scrapers.base import BaseScraper, EventItem
from event_collector.city_normalizer import find_city_in_text
from event_collector.concurrency import gather_limited
from event_collector.known import is_known
from event_collector.retention import get_window, too_far
from event_collector.normalize import (
    clean_html,
    parse_datetime,
    build_dedup_key,
)

API_BASE = "https://findloyalty-api.oblaci.rs/api"
COLLECTION_URL = f"{API_BASE}/CardType/Collection?countryId=196"
DETAIL_URL = f"{API_BASE}/CardType?id={{id}}"
SITE_BASE = "https://efinity.rs"
TIMEOUT = 15

# NameEN (нижний регистр) категории или подкатегории efinity → группа. Площадки (ZAPPA, SAVA CENTAR, LOŽIONICA,
# BELGRADE 360 DECK) и NEW YEAR 2027 — не типы событий, их здесь нет намеренно.
CATEGORIES: dict[str, dict[str, str]] = {
    "sport":                 {"group": "sports",   "type": "match"},
    "ice hockey":            {"group": "sports",   "type": "match"},
    "theatre":               {"group": "theater",  "type": "performance"},
    "comedy":                {"group": "theater",  "type": "comedy"},
    "musical":               {"group": "theater",  "type": "musical"},
    "immersive":             {"group": "theater",  "type": "performance"},
    "drama":                 {"group": "theater",  "type": "drama"},
    "monodrama":             {"group": "theater",  "type": "drama"},
    "theatrical performances": {"group": "theater", "type": "performance"},
    "circus":                {"group": "circus",   "type": "performance"},
    "concerts":              {"group": "music",    "type": "concert"},
    "museum":                {"group": "museum",   "type": "exhibition"},
    "palace of science":     {"group": "science",  "type": "exhibition"},
    "clubbing":              {"group": "music",    "type": "performance"},
    "festivals":             {"group": "festival", "type": "festival"},
}

GROUP_TO_CATEGORY = {
    "music":    "Концерты",
    "theater":  "Кино/Театр",
    "museum":   "Выставки/Ярмарки",
    "science":  "Выставки/Ярмарки",
    "sports":   "Спорт/активный отдых",
    "circus":   "Кино/Театр",
    "festival": "Фестивали",
}
DEFAULT_EVENT_TYPE = "Концерты"

DEFAULT_CITY = "Belgrade"


_FEST_IN_TEXT = re.compile(r'fest', re.IGNORECASE)


class EfinityRsScraper(BaseScraper):
    source = 'efinityrs'

    def __init__(self, cinema: dict | None = None):
        pass

    async def fetch(self, known_urls: set[str] | None = None) -> list[EventItem]:
        logger.info("EFINITYRS_FETCH | start")
        self.listed_count = None
        known_urls = known_urls or set()

        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            try:
                resp = await client.get(COLLECTION_URL)
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                logger.error(f"EFINITYRS_COLLECTION_ERROR | {e}")
                return []

            categories = data.get("Categories") or []
            card_types = data.get("CardTypes") or []
            logger.info(
                f"EFINITYRS_COLLECTION | cardTypes={len(card_types)} "
                f"categories={len(categories)}"
            )

            raw_events = self._flatten(card_types)
            logger.info(f"EFINITYRS_LEAF_EVENTS | count={len(raw_events)}")
            self.listed_count = len(raw_events)

            now = datetime.now(timezone.utc)
            future_days = get_window()[1]
            new_events = []
            skipped_stale = 0
            for ev in raw_events:
                eid = ev.get("Id")
                if not eid:
                    continue
                su = f"{SITE_BASE}/event-details/{ev.get('UrlName','')}_{eid}"
                if is_known(known_urls, su, parse_datetime(ev.get("Start"))):
                    continue
                if self._is_stale(ev, now, future_days):
                    skipped_stale += 1
                    continue
                new_events.append(ev)
            logger.info(
                f"EFINITYRS_NEW | listed={len(raw_events)} | new={len(new_events)} | "
                f"skipped_known={len(raw_events) - len(new_events) - skipped_stale} | "
                f"skipped_stale={skipped_stale}"
            )

            async def build(ev):
                try:
                    return await self._build_item(client, ev, categories)
                except Exception as e:
                    logger.warning(
                        f"EFINITYRS_EVENT_ERROR | id={ev.get('Id')} "
                        f"({ev.get('Name')}) | {e}"
                    )
                    return None

            built = await gather_limited(new_events, build)
            items: list[EventItem] = [it for it in built if it]

        logger.info(f"EFINITYRS_DONE | total_events={len(items)}")
        return items


    @staticmethod
    def _is_stale(ev: dict, now: datetime, future_days: int) -> bool:
        start = parse_datetime(ev.get("Start"))
        if start is None or start < now:
            return True
        return too_far(start.date(), future_days=future_days)

    def _flatten(self, card_types: list[dict]) -> list[dict]:
        events: list[dict] = []
        for ct in card_types:
            children = ct.get("Children") or []
            if ct.get("ChildCount", 0) > 0 and children:
                for child in children:
                    merged = dict(child)
                    if not (child.get("Categories") or []):
                        merged["Categories"] = ct.get("Categories")
                    if not (child.get("Subcategories") or []):
                        merged["Subcategories"] = ct.get("Subcategories")
                    events.append(merged)
            else:
                events.append(ct)
        return events

    async def _build_item(
        self,
        client: httpx.AsyncClient,
        ev: dict,
        categories: list[dict],
    ) -> EventItem | None:
        event_id = ev.get("Id")
        url_name = ev.get("UrlName", "")
        if not event_id:
            return None

        source_url = f"{SITE_BASE}/event-details/{url_name}_{event_id}"

        event_types, basis, hint = self.resolve_event_types(ev, categories)
        if basis == 'default':
            logger.warning(f"EFINITYRS_UNMAPPED_CATEGORY | id={event_id} | {ev.get('What') or ev.get('Name')} | "
                           f"категории сайта: {hint or '—'} → {event_types}")

        details = None
        try:
            dresp = await client.get(DETAIL_URL.format(id=event_id))
            dresp.raise_for_status()
            details = dresp.json()
        except Exception as e:
            logger.warning(f"EFINITYRS_DETAIL_ERROR | id={event_id} | {e}")

        title = ev.get("What") or ev.get("Name") or ""
        if not title:
            return None

        starts_at = parse_datetime(ev.get("Start"))
        if not starts_at:
            logger.debug(f"EFINITYRS_NO_DATE | id={event_id} | {title}")
            return None

        now = datetime.now(timezone.utc)
        if self._is_stale(ev, now, get_window()[1]):
            logger.debug(
                f"EFINITYRS_STALE | id={event_id} | {title} | {starts_at.date()}"
            )
            return None

        description = self._extract_description(details)
        venue = ev.get("Where")
        city = find_city_in_text(venue, default=DEFAULT_CITY)
        images = [ev["Image"]] if ev.get("Image") else []

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
            category_basis=basis,
            category_hint=hint,
        )

    def resolve_event_types(self, ev: dict, categories: list[dict]) -> tuple[list[str], str, str | None]:
        """(категории, основание, подсказка). Смотрим ВСЕ категории и подкатегории события: сначала категории,
        затем подкатегории; каждая известная даёт свою категорию, повторы убираются, первая — главная.
        Площадки и «NEW YEAR» в таблице не значатся и ничего не определяют. Ни одной известной →
        «Концерты» с основанием 'default' (в лог идёт EFINITYRS_UNMAPPED_CATEGORY)."""
        by_id = {c.get("Id"): c for c in categories}
        sub_by_id = {s.get("Id"): s for c in categories for s in (c.get("Subcategories") or [])}
        cat_names = [by_id[i].get("NameEN") for i in (ev.get("Categories") or []) if i in by_id]
        sub_names = [sub_by_id[i].get("NameEN") for i in (ev.get("Subcategories") or []) if i in sub_by_id]
        names = [n for n in cat_names + sub_names if n]
        hint = " / ".join(names) or None
        result: list[str] = []
        for name in names:
            match = CATEGORIES.get(name.lower())
            if match:
                label = GROUP_TO_CATEGORY.get(match["group"], DEFAULT_EVENT_TYPE)
                if label not in result:
                    result.append(label)
        if result:
            # Тег FESTIVALS у efinity стоит общим пакетом «FESTIVALS / CONCERTS / MUSIC / POP & ROCK» на крупных
            # сольных концертах (Seka Aleksić, Jason Derulo...). Если Фестивали — не единственная категория и в названии
            # или ссылке нет «fest», считаем тег ненадёжным: оставляем остальные категории, событие отдаём на проверку AI.
            fest = GROUP_TO_CATEGORY["festival"]
            if fest in result and len(result) > 1 and not _FEST_IN_TEXT.search(
                    f"{ev.get('What') or ev.get('Name') or ''} {ev.get('UrlName') or ''}"):
                return [c for c in result if c != fest], 'aggregate', hint
            return result, 'site', hint
        return [DEFAULT_EVENT_TYPE], 'default', hint

    def _extract_description(self, details: dict | None) -> str | None:
        if not details:
            return None
        ads = details.get("Advertisements") or []
        for a in ads:
            desc = a.get("Description") or a.get("DescriptionSr")
            if desc:
                return clean_html(desc)
        return None
