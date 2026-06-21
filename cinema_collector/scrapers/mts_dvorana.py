import re
from datetime import datetime, date
from loguru import logger
from bs4 import BeautifulSoup
from zoneinfo import ZoneInfo

from cinema_collector.scrapers.base import BaseScraper, CinemaEvent, http_get_with_retry
from shared.constants import MOVIE_DESC_SHORT_COLLECT_LENGTH, MOVIE_DESC_FULL_COLLECT_LENGTH

BELGRADE_TZ = ZoneInfo('Europe/Belgrade')
BASE_URL = "https://www.mtsdvorana.rs"
TIMEOUT = 15

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "sr-RS,sr;q=0.9,en;q=0.8",
}

TIME_RE = re.compile(r'^(\d{1,2}):(\d{2})$')


def _get_soup(url: str) -> BeautifulSoup | None:
    r = http_get_with_retry(url, headers=HEADERS, timeout=TIMEOUT)
    if r is None:
        return None
    return BeautifulSoup(r.text, "html.parser")


def _clean(el) -> str:
    return el.get_text(" ", strip=True) if el else ""


def _fetch_available_dates(soup: BeautifulSoup) -> list[str]:
    dates = []
    today = date.today()

    for a in soup.select("a[href*='?date=']"):
        href = a.get("href", "")
        m = re.search(r'\?date=(\d{4}-\d{2}-\d{2})', href)
        if m:
            date_str = m.group(1)
            try:
                d = date.fromisoformat(date_str)
                if d >= today:
                    dates.append(date_str)
            except ValueError:
                continue

    seen = set()
    result = []
    for d in dates:
        if d not in seen:
            seen.add(d)
            result.append(d)

    return sorted(result)


def _fetch_movie_detail(movie_url: str) -> dict:
    result = {'description_full': '', 'movie_genre': ''}

    if not movie_url:
        return result

    soup = _get_soup(movie_url)
    if not soup:
        return result

    desc_el = soup.select_one('div.description')
    if desc_el:
        result['description_full'] = desc_el.get_text(strip=True)

    for li in soup.select('ul.info-list li'):
        span = li.select_one('span')
        if span and 'Žanr' in span.get_text():
            genre_text = li.get_text(strip=True).replace(span.get_text(strip=True), '').strip()
            result['movie_genre'] = genre_text
            break

    return result


class MtsDvoranaScraper(BaseScraper):
    source = 'mts_dvorana'

    def __init__(self, cinema: dict):
        self.cinema = cinema
        self._detail_cache: dict[str, dict] = {}

    def fetch(self) -> list[CinemaEvent]:
        logger.info("MTS_FETCH | start")

        main_soup = _get_soup(f"{BASE_URL}/filmovi/repertoar")
        if not main_soup:
            return []

        dates = _fetch_available_dates(main_soup)
        if not dates:
            logger.warning("MTS_NO_DATES")
            return []

        logger.info(f"MTS_DATES | count={len(dates)} | first={dates[0]} | last={dates[-1]}")

        all_events = []
        for date_str in dates:
            events = self._fetch_date(date_str)
            all_events.extend(events)

        logger.info(f"MTS_DONE | total_events={len(all_events)}")
        return all_events

    def _fetch_date(self, date_str: str) -> list[CinemaEvent]:
        url = f"{BASE_URL}/filmovi/repertoar?date={date_str}"
        soup = _get_soup(url)
        if not soup:
            return []

        events = []
        for item in soup.select("div.movie-item"):
            movie_events = self._parse_movie(item, date_str)
            events.extend(movie_events)

        logger.debug(f"MTS_DATE | date={date_str} | events={len(events)}")
        return events

    def _parse_movie(self, item, date_str: str) -> list[CinemaEvent]:
        title_el = item.select_one("h2.movie-item-title a")
        movie_title_serbian = _clean(title_el)
        if not movie_title_serbian:
            return []

        orig_el = item.select_one("h3.original-title")
        movie_title = _clean(orig_el)

        if not movie_title or movie_title == movie_title_serbian:
            movie_title = movie_title_serbian
            movie_title_serbian = ""

        poster_url = ""
        img = item.select_one("img.img-fluid")
        if img:
            poster_url = img.get("src") or ""

        movie_url = ""
        link_el = item.select_one("a.movie-item-image[href*='/film/']")
        if link_el:
            href = link_el.get("href", "")
            movie_url = href if href.startswith("http") else BASE_URL + href

        if movie_url and movie_url not in self._detail_cache:
            logger.debug(f"MTS_DETAIL_FETCH | {movie_url}")
            self._detail_cache[movie_url] = _fetch_movie_detail(movie_url)

        detail = self._detail_cache.get(movie_url, {})
        description_full = detail.get('description_full', '')
        movie_genre = detail.get('movie_genre', '')
        description_short = description_full[:MOVIE_DESC_SHORT_COLLECT_LENGTH] if description_full else ''

        events = []

        proj_block = item.select_one(".projections-time")
        if not proj_block:
            return []

        for proj in proj_block.select(".projection-item"):
            time_el = proj.select_one(".date")
            time_str = _clean(time_el)
            if not TIME_RE.match(time_str):
                continue

            a_el = proj.select_one("a.time-btn[href]")
            if a_el:
                href = a_el.get("href", "")
                ticket_url = href if href.startswith("http") else BASE_URL + href
            else:
                ticket_url = movie_url or self.cinema.get('url', '')

            try:
                dt = datetime.fromisoformat(f"{date_str}T{time_str}:00")
                dt = dt.replace(tzinfo=BELGRADE_TZ)
            except ValueError:
                continue

            events.append(CinemaEvent(
                cinema_name=self.cinema['name'],
                cinema_maps_url=self.cinema.get('maps_url', ''),
                cinema_url=self.cinema.get('url', ''),
                movie_id='',
                movie_title=movie_title,
                movie_title_serbian=movie_title_serbian,
                movie_genre=movie_genre,
                movie_description_short=description_short,
                movie_description_full=description_full[:MOVIE_DESC_FULL_COLLECT_LENGTH],
                movie_poster_url=poster_url,
                showtime=dt,
                ticket_url=ticket_url,
                source=self.source,
            ))

        return events
