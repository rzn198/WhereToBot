import urllib.request
import urllib.error
import json
import time
from datetime import datetime
from loguru import logger
from zoneinfo import ZoneInfo

from cinema_collector.scrapers.base import BaseScraper, CinemaEvent
from shared.constants import MOVIE_DESC_SHORT_COLLECT_LENGTH, MOVIE_DESC_FULL_COLLECT_LENGTH

BELGRADE_TZ = ZoneInfo('Europe/Belgrade')
API_BASE = "https://app.cineplexx.rs/api"
TICKET_BASE = "https://www.cineplexx.rs/purchase/wizard"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "sr-RS,sr;q=0.9,en;q=0.8",
    "Origin": "https://www.cineplexx.rs",
    "Referer": "https://www.cineplexx.rs/",
}
TIMEOUT = 15


MAX_RETRIES = 2
RETRY_DELAY = 3.0


def _get(path: str) -> any:
    url = API_BASE + path
    req = urllib.request.Request(url, headers=HEADERS)

    last_error = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                last_error = e
                if attempt < MAX_RETRIES:
                    logger.warning(
                        f"CINEPLEXX_HTTP_RETRY | code={e.code} | attempt={attempt + 1}/{MAX_RETRIES} | {url}"
                    )
                    time.sleep(RETRY_DELAY)
                    continue
            logger.warning(f"CINEPLEXX_HTTP_ERROR | {e.code} | {url}")
            return None
        except Exception as e:
            last_error = e
            if attempt < MAX_RETRIES:
                logger.warning(
                    f"CINEPLEXX_RETRY | attempt={attempt + 1}/{MAX_RETRIES} | {e} | {url}"
                )
                time.sleep(RETRY_DELAY)
                continue

    logger.error(f"CINEPLEXX_ERROR | {last_error} | {url}")
    return None


class CineplexxScraper(BaseScraper):
    source = 'cineplexx'

    def __init__(self, cinemas: list[dict]):
        self.cinemas_by_id = {str(c['id']): c for c in cinemas}

    def fetch(self) -> list[CinemaEvent]:
        logger.info("CINEPLEXX_FETCH | start")

        dates_raw = _get("/v2/movies/filters/dates/list?location=0")
        if not dates_raw:
            logger.warning("CINEPLEXX_NO_DATES")
            return []

        dates = []
        for dt_str in dates_raw:
            try:
                dt = datetime.fromisoformat(dt_str)
                dates.append(dt.strftime('%Y-%m-%d'))
            except ValueError:
                continue

        logger.info(f"CINEPLEXX_DATES | count={len(dates)} | first={dates[0]} | last={dates[-1]}")

        all_events = []

        sessions_cache: dict[str, list] = {}

        for date_str in dates:
            movies_raw = _get(f"/v2/movies?date={date_str}&location=0")
            if not movies_raw:
                logger.debug(f"CINEPLEXX_NO_MOVIES | date={date_str}")
                continue

            logger.debug(f"CINEPLEXX_MOVIES | date={date_str} | count={len(movies_raw)}")

            for movie in movies_raw:
                movie_id = movie.get("id", "")
                if not movie_id:
                    continue

                movie_title = movie.get("titleOriginalCalculated", "") or movie.get("title", "")
                movie_title_serbian = movie.get("titleCalculated", "")
                genres = movie.get("genres", [])
                movie_genre = ", ".join(genres) if genres else ""
                movie_description_short = movie.get("descriptionShortCalculated", "") or ""
                movie_description_full = movie.get("descriptionCalculated", "") or ""
                movie_poster_url = movie.get("posterImage", "") or ""

                if movie_id not in sessions_cache:
                    sessions_data = _get(f"/v3/moviesweb/{movie_id}/sessions?location=0")
                    sessions_cache[movie_id] = sessions_data or []

                sessions_data = sessions_cache[movie_id]

                for day in sessions_data:
                    day_date_str = day.get("date", "")
                    try:
                        day_date = datetime.fromisoformat(day_date_str).strftime('%Y-%m-%d')
                    except ValueError:
                        continue

                    if day_date != date_str:
                        continue

                    for session in day.get("sessions", []):
                        showtime_str = session.get("showtime", "")
                        if not showtime_str:
                            continue

                        cinema_id = str(session.get("cinemaId", ""))
                        cinema = self.cinemas_by_id.get(cinema_id)
                        if not cinema:
                            continue

                        session_composite_id = session.get("id", "")
                        ticket_url = f"{TICKET_BASE}/{session_composite_id}/tickets" if session_composite_id else ""

                        try:
                            dt = datetime.fromisoformat(showtime_str)
                            if dt.tzinfo is None:
                                dt = dt.replace(tzinfo=BELGRADE_TZ)
                        except ValueError:
                            logger.warning(f"CINEPLEXX_BAD_DT | {showtime_str}")
                            continue

                        all_events.append(CinemaEvent(
                            cinema_name=cinema['name'],
                            cinema_maps_url=cinema.get('maps_url', ''),
                            cinema_url=cinema.get('url', ''),
                            movie_id=movie_id,
                            movie_title=movie_title,
                            movie_title_serbian=movie_title_serbian,
                            movie_genre=movie_genre,
                            movie_description_short=movie_description_short[:MOVIE_DESC_SHORT_COLLECT_LENGTH] if movie_description_short else '',
                            movie_description_full=movie_description_full[:MOVIE_DESC_FULL_COLLECT_LENGTH] if movie_description_full else '',
                            movie_poster_url=movie_poster_url,
                            showtime=dt,
                            ticket_url=ticket_url,
                            source=self.source,
                        ))

        logger.info(f"CINEPLEXX_DONE | total_events={len(all_events)}")
        return all_events
