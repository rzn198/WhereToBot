import time
import requests
from datetime import datetime, date, timedelta
from loguru import logger
from zoneinfo import ZoneInfo

from cinema_collector.scrapers.base import BaseScraper, CinemaEvent
from shared.constants import MOVIE_DESC_SHORT_COLLECT_LENGTH, MOVIE_DESC_FULL_COLLECT_LENGTH

BELGRADE_TZ = ZoneInfo('Europe/Belgrade')
API_URL = "https://stagetest.gart.rs:9393/TBeastAPI/api/public/getWebDataAndInformationAsync"
BASE_URL = "https://cinegrand-mcf.rs"
TIMEOUT = 15

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Content-Type": "application/json",
    "Origin": BASE_URL,
    "Referer": BASE_URL + "/",
}


MAX_RETRIES = 2
RETRY_DELAY = 3.0


def _post(body: dict) -> dict | None:
    last_error = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            r = requests.post(API_URL, json=body, headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            return r.json()
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            if status == 429 or (status is not None and status >= 500):
                last_error = e
                if attempt < MAX_RETRIES:
                    logger.warning(
                        f"CINEGRAND_HTTP_RETRY | code={status} | attempt={attempt + 1}/{MAX_RETRIES}"
                    )
                    time.sleep(RETRY_DELAY)
                    continue
            logger.error(f"CINEGRAND_HTTP_ERROR | code={status} | {e}")
            return None
        except Exception as e:
            last_error = e
            if attempt < MAX_RETRIES:
                logger.warning(
                    f"CINEGRAND_RETRY | attempt={attempt + 1}/{MAX_RETRIES} | {e}"
                )
                time.sleep(RETRY_DELAY)
                continue

    logger.error(f"CINEGRAND_ERROR | {last_error}")
    return None


class CineGrandScraper(BaseScraper):
    source = 'cinegrand'

    def __init__(self, cinema: dict):
        self.cinema = cinema
        self.location_id = cinema.get('location_id', 1)

    def fetch(self) -> list[CinemaEvent]:
        logger.info("CINEGRAND_FETCH | start")

        data = _post({
            "happeningType": 1,
            "webTableIncludes": [1],
            "locationId": self.location_id,
        })
        film_list = (data or {}).get("webHappeningsDetails") or []

        if not film_list:
            logger.warning(f"CINEGRAND_NO_FILMS | location={self.location_id}")
            return []

        logger.info(f"CINEGRAND_FILMS | count={len(film_list)}")

        today = date.today()
        end_date = today + timedelta(days=30)
        date_from = f"{today} 00:00:00"
        date_to = f"{end_date} 23:59:59"

        events = []

        for item in film_list:
            happening_stub = item.get("happening") or {}
            hid = happening_stub.get("id")
            if not hid:
                continue

            detail_data = _post({
                "happeningType": 1,
                "happeningId": hid,
                "webTableIncludes": [10],
                "dateFrom": date_from,
                "dateTo": date_to,
                "locationId": self.location_id,
            })
            details = (detail_data or {}).get("webHappeningsDetails") or []
            detail = details[0] if details else None

            if not detail:
                logger.debug(f"CINEGRAND_NO_DETAIL | hid={hid}")
                continue

            happening = detail.get("happening") or {}
            web_happening = detail.get("webHappening") or {}
            projections = detail.get("happeningProjections") or []

            if not projections:
                logger.debug(f"CINEGRAND_NO_PROJ | hid={hid}")
                continue

            movie_title = (happening.get("originalName") or "").strip()
            movie_title_serbian = (happening.get("name") or "").strip()

            if not movie_title or movie_title == movie_title_serbian:
                movie_title = movie_title_serbian
                movie_title_serbian = ""

            if not movie_title:
                continue

            poster_url = (web_happening.get("posterURL") or "").strip()
            description_full = (web_happening.get("synopsis") or "").strip()
            description_short = (web_happening.get("shortSynopsis") or "").strip()

            if not description_short and description_full:
                description_short = description_full[:MOVIE_DESC_SHORT_COLLECT_LENGTH]

            ticket_base = f"{BASE_URL}/happening/{hid}"

            for proj in projections:
                dt_str = proj.get("startDatetime", "")
                if not dt_str:
                    continue

                try:
                    dt = datetime.fromisoformat(dt_str)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=BELGRADE_TZ)
                except ValueError:
                    logger.warning(f"CINEGRAND_BAD_DT | {dt_str}")
                    continue

                ticket_url = f"{BASE_URL}/happening-details/{hid}"

                events.append(CinemaEvent(
                    cinema_name=self.cinema['name'],
                    cinema_maps_url=self.cinema.get('maps_url', ''),
                    cinema_url=self.cinema.get('url', ''),
                    movie_id=str(hid),
                    movie_title=movie_title,
                    movie_title_serbian=movie_title_serbian,
                    movie_genre='',  # API не возвращает жанр
                    movie_description_short=description_short[:MOVIE_DESC_SHORT_COLLECT_LENGTH] if description_short else '',
                    movie_description_full=description_full[:MOVIE_DESC_FULL_COLLECT_LENGTH] if description_full else '',
                    movie_poster_url=poster_url,
                    showtime=dt,
                    ticket_url=ticket_url,
                    source=self.source,
                ))

        logger.info(f"CINEGRAND_DONE | total_events={len(events)}")
        return events
