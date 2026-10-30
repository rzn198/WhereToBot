# event_collector/scrapers/polako.py
# Порт Node RootHandler (plugins/polako) на Python.
# Источник: Directus API (polako-prod ...ondigitalocean.app). Чистый JSON.
#
# Поток (один проход, без detail-запросов):
#   1. GET ?aggregate[count]=id&filter=...     -> total count
#   2. пагинация ?fields=...&limit&offset&sort&filter -> страницы событий
#   3. каждое событие маппится сразу в EventItem
#
# Особенности:
#   - мультиязычность translations: en > sr > ru, фолбэк на name
#   - город приходит явно (location.city.name)
#   - горизонт дат: сегодня .. +3 года, только status=scheduled
#   - is_permanent неприменим (все события с датой) -> всегда False

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx
from loguru import logger

from event_collector.scrapers.base import BaseScraper, EventItem
from event_collector.normalize import clean_html, parse_datetime, build_dedup_key
from event_collector.city_normalizer import find_city_in_text
from collector.categorizer_v2 import categorize

BELGRADE_TZ = ZoneInfo('Europe/Belgrade')

API_BASE = "https://polako-prod-5qyaj.ondigitalocean.app/data/events"
ASSETS_BASE = "https://polako-prod-5qyaj.ondigitalocean.app/assets"
SITE_BASE = "https://polakohedonist.club/ru/events"
TIMEOUT = 20
PAGE_SIZE = 500
PAGE_DELAY = 2.0   # пауза между страницами (как rateLimit в оригинале)
YEARS_AHEAD = 3

# Поля запроса к Directus (slug НЕ запрашиваем явно — вызывает 403,
# но приходит в ответе автоматически).
API_FIELDS = ",".join([
    "id", "name", "start_date", "status", "base_currency", "duration",
    "visit_price", "price_type", "for_adults",
    "image_cover.id", "image_cover.filename_disk",
    "location.name", "location.address_link",
    "translations.event_name", "translations.event_description",
    "translations.language",
    "translations.image_cover.id", "translations.image_cover.filename_disk",
    "description",
])

LANG_PRIORITY = ["ru", "en", "sr"]

# якоря-«мусор» для ссылок [текст](url): если текст такой — убираем ссылку
# целиком; иначе оставляем текст, url выбрасываем
_JUNK_ANCHORS = {
    "google maps", "maps", "карта", "на карте", "карте", "map",
    "google map", "локация", "location", "ссылка", "link", "здесь",
    "видео на youtube", "youtube", "подробнее",
}

_MD_LINK = re.compile(r'\[([^\]]*)\]\(([^)]*)\)')      # [текст](url)
_BARE_URL = re.compile(r'https?://\S+')                # голый url
_MENTION = re.compile(r'(?<!\w)@[\w.]+')               # @упоминание
_HEADING = re.compile(r'^\s{0,3}#{1,6}\s*', re.M)      # # заголовок
_QUOTE = re.compile(r'^\s{0,3}>\s?', re.M)             # > цитата
_LIST_MARK = re.compile(r'^\s{0,3}(?:[-*+]|\d+\.)\s+', re.M)  # - / 1. список
_BOLD_IT = re.compile(r'(\*{1,3}|_{1,3})(.+?)\1')      # **ж**/*к*/_к_
_EMOJI = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF"
    "\U00002190-\U000021FF\U00002B00-\U00002BFF\uFE0F]"
)
_MULTISPACE = re.compile(r'[ \t]{2,}')
_MULTINL = re.compile(r'\n{3,}')


def _link_sub(m: re.Match) -> str:
    anchor = (m.group(1) or "").strip()
    low = anchor.lower()
    # мусорный якорь или сам текст — это url → убрать целиком
    if not anchor or low in _JUNK_ANCHORS or anchor.startswith(("http://", "https://")):
        return ""
    return anchor       # осмысленный текст оставляем, url выбрасываем


def _markdown_to_text(text: str | None) -> str | None:
    """Markdown -> чистый текст. Гибрид по ссылкам: осмысленный текст
    якоря оставляем, мусор-якоря/url/@упоминания/эмодзи убираем.
    Работает на СЫРОМ тексте (переносы \\n целы) — раскодирует HTML-сущности
    сам, т.к. clean_html до него не вызывается."""
    if not text:
        return text
    import html as _html
    t = _html.unescape(text)            # &amp; &nbsp; и пр. (clean_html не звался)
    t = _MD_LINK.sub(_link_sub, t)     # ссылки — до всего (внутри url со спецсимволами)
    t = _BARE_URL.sub("", t)            # голые url
    t = _MENTION.sub("", t)             # @упоминания
    t = _BOLD_IT.sub(r'\2', t)          # снять **/*/_
    t = _HEADING.sub("", t)             # заголовки
    t = _QUOTE.sub("", t)               # цитаты
    t = _LIST_MARK.sub("", t)           # маркеры списков
    t = _EMOJI.sub("", t)               # эмодзи
    t = t.replace("·", " ")             # разделитель-точка
    # схлопывание пробелов/переносов
    t = _MULTISPACE.sub(" ", t)
    t = _MULTINL.sub("\n\n", t)
    # убрать пробелы у краёв строк
    t = "\n".join(line.strip() for line in t.split("\n"))
    return t.strip() or None


class PolakoScraper(BaseScraper):
    source = 'polako'

    def __init__(self, *args, **kwargs):
        pass

    def _build_filter(self) -> str:
        """Фильтр Directus: будущие события сегодня..+3 года, status=scheduled."""
        today = datetime.now(BELGRADE_TZ)
        year = today.year + YEARS_AHEAD
        frm = today.strftime('%Y-%m-%d') + " 00:00:00"
        to = f"{year}-{today.strftime('%m-%d')} 23:59:59"
        return json.dumps({
            "_and": [
                {"_or": [{"hidden": {"_eq": False}}, {"hidden": {"_null": True}}]},
            ],
            "status": {"_in": ["scheduled"]},
            "country_codes": {"_contains": "RS"},
            "start_date": {"_between": [frm, to]},
        })

    async def fetch(self) -> list[EventItem]:
        logger.info("POLAKO_FETCH | start")
        api_filter = self._build_filter()

        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            # Шаг 1: total count
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

            # Шаг 2: пагинация
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

        # source_url по id: сайт открывает /events/{id} напрямую (проверено:
        # HTTP 200). slug приходит нестабильно (у ~500/700 пуст) и запрещён к
        # явному запросу (403), поэтому используем id — он есть всегда.
        eid = ev.get("id")
        source_url = f"{SITE_BASE}/{eid}"

        # title/description: перевод en>sr>ru, фолбэк name
        tr = self._pick_translation(ev.get("translations"))
        title = (tr or {}).get("event_name") or ev.get("name")
        if not title:
            return None
        # описание: перевод, фолбэк top-level. markdown-чистку делаем на
        # СЫРОМ тексте — переносы \n\n ещё целы, по ним работают ^-якоря
        # заголовков/списков. clean_html схлопнул бы \n в пробел, поэтому
        # применяем его ПОСЛЕ (внутри _markdown_to_text — раскодировка сущностей).
        raw_desc = (tr or {}).get("event_description") or ev.get("description")
        description = _markdown_to_text(raw_desc)

        starts_at = parse_datetime(ev.get("start_date"))
        if not starts_at:
            return None
        ends_at = self._calc_end(starts_at, ev.get("duration"))

        loc = ev.get("location") or {}
        venue = loc.get("name")
        # API фильтрует по country_codes=RS -> все события сербские.
        # location.city.country.code больше не приходит (структура изменилась),
        # прежняя проверка отбрасывала всё. Город ищем в venue/location.name,
        # дефолт Belgrade; страна RS.
        country = "RS"
        city = find_city_in_text(venue, default="Belgrade")

        # категоризация как в messages: title + description, мульти-лейбл
        cat_text = title
        if description:
            cat_text = f"{title} {description}"
        event_types = categorize(cat_text, channel='polako', position=0)

        # картинка: выбранная tr -> верхний уровень -> любая непустая
        # translation (у разных языков картинка может отличаться/отсутствовать)
        images = []
        cover = (tr or {}).get("image_cover") or ev.get("image_cover")
        if not (cover and cover.get("id")):
            for t in (ev.get("translations") or []):
                c = t.get("image_cover")
                if c and c.get("id"):
                    cover = c
                    break
        if cover and cover.get("id"):
            # filename_disk = "{id}.jpg/.png" (с расширением) — предпочтительно;
            # фолбэк на голый id, если filename_disk почему-то нет
            asset = cover.get("filename_disk") or cover["id"]
            images.append(f"{ASSETS_BASE}/{asset}")

        tickets = [{
            "price": ev.get("visit_price") or "0.00",
            "currency": ev.get("base_currency") or "RSD",
            "type": ev.get("price_type") or "regular",
            "url": ev.get("book_link") or source_url,
        }]

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

    def _calc_end(self, start: datetime, duration_min) -> datetime | None:
        if not duration_min:
            return None
        try:
            from datetime import timedelta
            return start + timedelta(minutes=int(duration_min))
        except (TypeError, ValueError):
            return None
