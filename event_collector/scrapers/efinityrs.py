# event_collector/scrapers/efinityrs.py
# Порт Node RootHandler (plugins/efinityrs) на Python.
# Источник: findloyalty API (oblaci.rs). Чистый JSON, без авторизации.
#
# Поток (один проход, N+1 запросов):
#   1. GET /api/CardType/Collection?countryId=196  -> {CardTypes[], Categories[]}
#   2. Разворот parent->children (наследование Categories/Subcategories)
#   3. Для каждого события: resolveCategory + GET /api/CardType?id={id} (детали)
#   4. Маппинг в EventItem через normalize

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx
from loguru import logger

from event_collector.scrapers.base import BaseScraper, EventItem
from event_collector.city_normalizer import find_city_in_text
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
DETAIL_DELAY = 0.1  # пауза между detail-запросами, щадим API

# Маппинг категорий (сырьём, как в Node). Ключ = lower(NameEN).
CATEGORIES: dict[str, dict[str, str]] = {
    "sport":            {"group": "sports",  "type": "match"},
    "comedy":           {"group": "theater", "type": "comedy"},
    "musical":          {"group": "theater", "type": "musical"},
    "immersive":        {"group": "theater", "type": "performance"},
    "circus":           {"group": "circus",  "type": "performance"},
    "drama":            {"group": "theater", "type": "drama"},
    "concerts":         {"group": "music",   "type": "concert"},
    "museum":           {"group": "museum",  "type": "exhibition"},
    "palace of science":{"group": "science", "type": "exhibition"},
    "clubbing":         {"group": "music",   "type": "performance"},
}
DEFAULT_CATEGORY = {"group": "music", "type": "concert"}

# event_group -> русская категория (event_types). Все возможные group из CATEGORIES.
GROUP_TO_CATEGORY = {
    "music":   "Концерты",
    "theater": "Кино/Театр",
    "museum":  "Выставки/Ярмарки",
    "science": "Выставки/Ярмарки",
    "sports":  "Спорт/активный отдых",
    "circus":  "Кино/Театр",
}
DEFAULT_EVENT_TYPE = "Концерты"

# efinityrs — белградский источник (координаты подтверждают Белград)
DEFAULT_CITY = "Belgrade"


class EfinityRsScraper(BaseScraper):
    source = 'efinityrs'

    def __init__(self, cinema: dict | None = None):
        # cinema-параметр не нужен efinityrs (API сам отдаёт всё), оставлен
        # для единообразия сигнатуры с другими скраперами.
        pass

    async def fetch(self, known_urls: set[str] | None = None) -> list[EventItem]:
        logger.info("EFINITYRS_FETCH | start")
        known_urls = known_urls or set()

        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            # --- Шаг 1: коллекция ---
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

            # --- Шаг 2: разворот parent->children ---
            raw_events = self._flatten(card_types)
            logger.info(f"EFINITYRS_LEAF_EVENTS | count={len(raw_events)}")

            # --- Шаг 3-4: детали + маппинг ---
            # Отсекаем известные ДО detail-запроса: source_url строится из
            # UrlName+Id, которые есть в raw_event (до detail).
            new_events = []
            for ev in raw_events:
                eid = ev.get("Id")
                if not eid:
                    continue
                su = f"{SITE_BASE}/event-details/{ev.get('UrlName','')}_{eid}"
                if su not in known_urls:
                    new_events.append(ev)
            logger.info(
                f"EFINITYRS_NEW | listed={len(raw_events)} | new={len(new_events)} | "
                f"skipped_known={len(raw_events) - len(new_events)}"
            )

            items: list[EventItem] = []
            for ev in new_events:
                try:
                    item = await self._build_item(client, ev, categories)
                    if item:
                        items.append(item)
                except Exception as e:
                    logger.warning(
                        f"EFINITYRS_EVENT_ERROR | id={ev.get('Id')} "
                        f"({ev.get('Name')}) | {e}"
                    )
                await asyncio.sleep(DETAIL_DELAY)

        logger.info(f"EFINITYRS_DONE | total_events={len(items)}")
        return items

    # ------------------------------------------------------------------

    def _flatten(self, card_types: list[dict]) -> list[dict]:
        """Разворачивает родительские события в листовые (children)."""
        events: list[dict] = []
        for ct in card_types:
            children = ct.get("Children") or []
            if ct.get("ChildCount", 0) > 0 and children:
                for child in children:
                    merged = dict(child)
                    # наследуем категории родителя, если у ребёнка пусто
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

        cat = self._resolve_category(ev, categories)

        # --- детали (координаты, цены, описание) ---
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
            # без даты события нам не нужны (лента строится по дате)
            logger.debug(f"EFINITYRS_NO_DATE | id={event_id} | {title}")
            return None

        ends_at = parse_datetime(ev.get("End"))

        # Классификация по времени:
        #   Start в будущем              -> обычное событие
        #   Start в прошлом + End=None   -> постоянная экспозиция (is_permanent)
        #   Start в прошлом + End задан  -> прошедшее разовое событие, отсекаем
        now = datetime.now(timezone.utc)
        is_permanent = False
        if starts_at < now:
            if ends_at is None:
                is_permanent = True
                logger.debug(
                    f"EFINITYRS_PERMANENT | id={event_id} | {title}"
                )
            else:
                logger.debug(
                    f"EFINITYRS_PAST | id={event_id} | {title} | "
                    f"{starts_at.date()}..{ends_at.date()}"
                )
                return None

        description = self._extract_description(details)
        venue = ev.get("Where")
        # город из venue (Where); не нашли известный -> Belgrade
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
            event_types=[GROUP_TO_CATEGORY.get(cat["group"], DEFAULT_EVENT_TYPE)],
            images=images,
            dedup_key=build_dedup_key(title, starts_at, city),
        )

    def _resolve_category(self, ev: dict, categories: list[dict]) -> dict:
        cat_ids = ev.get("Categories") or []
        sub_ids = ev.get("Subcategories") or []
        category_id = cat_ids[0] if cat_ids else None
        subcategory_id = sub_ids[0] if sub_ids else None

        if not category_id:
            return DEFAULT_CATEGORY

        category = next(
            (c for c in categories if c.get("Id") == category_id), None
        )
        if not category:
            return DEFAULT_CATEGORY

        # сперва пробуем подкатегорию
        if subcategory_id:
            sub = next(
                (s for s in (category.get("Subcategories") or [])
                 if s.get("Id") == subcategory_id),
                None,
            )
            if sub:
                match = CATEGORIES.get((sub.get("NameEN") or "").lower())
                if match:
                    return match

        match = CATEGORIES.get((category.get("NameEN") or "").lower())
        return match or DEFAULT_CATEGORY

    def _extract_description(self, details: dict | None) -> str | None:
        if not details:
            return None
        ads = details.get("Advertisements") or []
        for a in ads:
            desc = a.get("Description") or a.get("DescriptionSr")
            if desc:
                return clean_html(desc)
        return None
