# cinema_collector/scrapers/cinegrand.py
# CineGrand (cinegrand-mcf.rs). С ноября 2026 сайт переписан, API другое (v2, открытое):
#   GET {api}/happenings/films-now-playing?locationId=N   -> фильмы кинотеатра
#   GET {api}/happenings/{id}                              -> описание, оригинальное название, жанры
#   GET {api}/happenings/{id}/projections                  -> сеансы ВСЕХ кинотеатров сети
# Параметры запроса сеансов сервер игнорирует, поэтому кинотеатр и срок фильтруем сами
# (так делает и сам сайт). Прежний адрес stagetest.gart.rs:9393 (TBeastAPI) удалён из DNS.

import html
import re
import time
from datetime import datetime, timedelta

import requests
from loguru import logger
from zoneinfo import ZoneInfo

from cinema_collector.scrapers.base import BaseScraper, CinemaEvent
from shared.constants import MOVIE_DESC_SHORT_COLLECT_LENGTH, MOVIE_DESC_FULL_COLLECT_LENGTH

BELGRADE_TZ = ZoneInfo('Europe/Belgrade')
DEFAULT_API_URL = "https://api.cinegrand-mcf.rs/api/v2"
BASE_URL = "https://cinegrand-mcf.rs"
TIMEOUT = 15
DAYS_AHEAD = 30            # как и раньше: сеансы на 30 дней вперёд
MAX_RETRIES = 2
RETRY_DELAY = 3.0
REQUEST_DELAY = 0.2        # пауза между запросами, щадим API
MAX_FAIL_RATIO = 0.25      # если не удалось загрузить больше четверти фильмов — данные не обновляем

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

_BLOCK_END = re.compile(r'(?i)<br\s*/?>|</p>|</div>|</li>')
_TAG = re.compile(r'<[^>]+>')
_MANY_NEWLINES = re.compile(r'\n{3,}')


def html_to_text(raw: str | None) -> str:
    """HTML описания (<p>, &scaron; ...) -> обычный текст с абзацами."""
    if not raw:
        return ''
    text = _TAG.sub('', _BLOCK_END.sub('\n', raw))
    text = html.unescape(text).replace('\xa0', ' ')
    text = '\n'.join(line.strip() for line in text.splitlines())
    return _MANY_NEWLINES.sub('\n\n', text).strip()


def _get(url: str, params: dict | None = None) -> dict | None:
    """GET с повторами для 429/5xx и сетевых сбоев. Возвращает JSON или None."""
    last_error = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            return r.json()
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            last_error = e
            if status == 429 or (status is not None and status >= 500):
                if attempt < MAX_RETRIES:
                    logger.warning(f"CINEGRAND_HTTP_RETRY | code={status} | attempt={attempt + 1}/{MAX_RETRIES} | {url}")
                    time.sleep(RETRY_DELAY)
                    continue
            logger.error(f"CINEGRAND_HTTP_ERROR | code={status} | {url}")
            return None
        except Exception as e:
            last_error = e
            if attempt < MAX_RETRIES:
                logger.warning(f"CINEGRAND_RETRY | attempt={attempt + 1}/{MAX_RETRIES} | {url} | {e}")
                time.sleep(RETRY_DELAY)
                continue
    logger.error(f"CINEGRAND_ERROR | {url} | {last_error}")
    return None


def _parse_start(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        logger.warning(f"CINEGRAND_BAD_DT | {value}")
        return None
    return dt.replace(tzinfo=BELGRADE_TZ) if dt.tzinfo is None else dt


class CineGrandScraper(BaseScraper):
    source = 'cinegrand'

    def __init__(self, cinema: dict):
        self.cinema = cinema
        self.location_id = cinema.get('location_id', 1)
        self.api_url = (cinema.get('api_url') or DEFAULT_API_URL).rstrip('/')

    def fetch(self, now: datetime | None = None) -> list[CinemaEvent]:
        logger.info("CINEGRAND_FETCH | start")
        now = now or datetime.now(BELGRADE_TZ)
        window_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        window_end = window_start + timedelta(days=DAYS_AHEAD + 1)

        listing = _get(f"{self.api_url}/happenings/films-now-playing",
                       {"locationId": self.location_id})
        films = [
            f for f in ((listing or {}).get("data") or [])
            if f.get("id") and (f.get("type") in (None, "cinema"))
        ]
        if not films:
            logger.warning(f"CINEGRAND_NO_FILMS | location={self.location_id}")
            return []
        logger.info(f"CINEGRAND_FILMS | count={len(films)}")

        events: list[CinemaEvent] = []
        failed = 0
        for film in films:
            film_events = self._fetch_film(film, window_start, window_end)
            if film_events is None:
                failed += 1
            else:
                events.extend(film_events)
            time.sleep(REQUEST_DELAY)

        if failed / len(films) > MAX_FAIL_RATIO:
            # лучше оставить прежние данные, чем стереть афишу неполной загрузкой
            logger.error(f"CINEGRAND_TOO_MANY_FAILURES | failed={failed}/{len(films)} | данные не обновляются")
            return []

        logger.info(f"CINEGRAND_DONE | total_events={len(events)} | failed_films={failed}")
        return events

    def _fetch_film(self, film: dict, window_start: datetime, window_end: datetime) -> list[CinemaEvent] | None:
        """Сеансы одного фильма в нашем кинотеатре. None — не удалось загрузить сеансы."""
        hid = film["id"]
        proj_resp = _get(f"{self.api_url}/happenings/{hid}/projections")
        if proj_resp is None:
            return None

        sessions = []
        for p in proj_resp.get("data") or []:
            if p.get("locationId") != self.location_id:
                continue
            dt = _parse_start(p.get("startTime"))
            if dt is not None and window_start <= dt < window_end:
                sessions.append(dt)
        if not sessions:
            logger.debug(f"CINEGRAND_NO_PROJ | hid={hid}")
            return []

        # детали (описание, оригинальное название, жанры); при сбое — данные из списка
        detail_resp = _get(f"{self.api_url}/happenings/{hid}")
        info = (detail_resp or {}).get("data") or film

        serbian = (info.get("name") or film.get("name") or "").strip()
        original = (info.get("originalName") or "").strip()
        if not original or original == serbian:
            title, title_serbian = serbian, ""
        else:
            title, title_serbian = original, serbian
        if not title:
            return []

        full = html_to_text(info.get("synopsis"))
        short = html_to_text(info.get("shortSynopsis"))
        if not short and full:
            short = full[:MOVIE_DESC_SHORT_COLLECT_LENGTH]
        genres = ", ".join(g.get("name", "") for g in (info.get("genres") or []) if g.get("name"))
        slug = info.get("urlSlug") or film.get("urlSlug")
        ticket_url = f"{BASE_URL}/film/{slug}" if slug else BASE_URL

        return [
            CinemaEvent(
                cinema_name=self.cinema['name'],
                cinema_maps_url=self.cinema.get('maps_url', ''),
                cinema_url=self.cinema.get('url', ''),
                movie_id=str(hid),
                movie_title=title,
                movie_title_serbian=title_serbian,
                movie_genre=genres,
                movie_description_short=short[:MOVIE_DESC_SHORT_COLLECT_LENGTH],
                movie_description_full=full[:MOVIE_DESC_FULL_COLLECT_LENGTH],
                movie_poster_url=(info.get("posterUrl") or "").strip(),
                showtime=dt,
                ticket_url=ticket_url,
                source=self.source,
            )
            for dt in sorted(sessions)
        ]
