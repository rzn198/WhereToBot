import re
from datetime import datetime
from loguru import logger
from bs4 import BeautifulSoup
from zoneinfo import ZoneInfo

from cinema_collector.scrapers.base import BaseScraper, CinemaEvent, http_get_with_retry
from shared.constants import MOVIE_DESC_SHORT_COLLECT_LENGTH, MOVIE_DESC_FULL_COLLECT_LENGTH

BELGRADE_TZ = ZoneInfo('Europe/Belgrade')
BASE_URL = "https://www.tuck.rs"
REPERTOAR_URL = f"{BASE_URL}/repertoar/"
TIMEOUT = 15

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "sr-RS,sr;q=0.9,en;q=0.8",
}

TIME_RE = re.compile(r'([01]?\d|2[0-3]):[0-5]\d')

MONTHS_SR = {
    'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'maj': 5, 'jun': 6,
    'jul': 7, 'avg': 8, 'sep': 9, 'okt': 10, 'nov': 11, 'dec': 12
}


def _get_soup(url: str) -> BeautifulSoup | None:
    r = http_get_with_retry(url, headers=HEADERS, timeout=TIMEOUT)
    if r is None:
        return None
    return BeautifulSoup(r.text, "html.parser")


def _clean(el) -> str:
    return el.get_text(" ", strip=True) if el else ""


def _field(item, class_suffix: str) -> str:
    el = item.select_one(
        f".amy-movie-field-{class_suffix} .amy-movie-custom-field-content"
    )
    if el:
        return _clean(el)
    el = item.select_one(f".amy-movie-field-{class_suffix}")
    return _clean(el)


def _parse_date(date_str: str) -> str:
    m = re.search(r'(\d{1,2})\.\s+(\w+)\s+(\d{4})', date_str)
    if m:
        day = int(m.group(1))
        month_str = m.group(2).lower()[:3]
        year = int(m.group(3))
        month = MONTHS_SR.get(month_str)
        if month:
            return f"{year}-{month:02d}-{day:02d}"

    m = re.search(r'(\d{1,2})\.(\d{1,2})\.(\d{4})?', date_str)
    if m:
        day = int(m.group(1))
        mon = int(m.group(2))
        year = int(m.group(3)) if m.group(3) else datetime.now().year
        return f"{year}-{mon:02d}-{day:02d}"

    return ""


def _parse_title(raw_title: str) -> tuple[str, str]:
    if ' / ' in raw_title:
        parts = raw_title.split(' / ', 1)
        return parts[0].strip(), parts[1].strip()
    return '', raw_title.strip()


def _fetch_description(movie_url: str) -> tuple[str, str]:
    if not movie_url:
        return '', ''

    soup = _get_soup(movie_url)
    if not soup:
        return '', ''

    content_el = soup.select_one('.entry-content.e-content')
    if not content_el:
        content_el = soup.select_one('.entry-content')

    if not content_el:
        return '', ''

    text = content_el.get_text(' ', strip=True)

    if text.lower().startswith('opis'):
        text = text[4:].strip()

    if not text or len(text) < 20:
        return '', ''

    description_short = text[:MOVIE_DESC_SHORT_COLLECT_LENGTH]
    description_full = text[:MOVIE_DESC_FULL_COLLECT_LENGTH]

    return description_short, description_full


class TuckwoodScraper(BaseScraper):
    source = 'tuckwood'

    def __init__(self, cinema: dict):
        self.cinema = cinema
        self._desc_cache: dict[str, tuple[str, str]] = {}

    def fetch(self) -> list[CinemaEvent]:
        logger.info("TUCKWOOD_FETCH | start")

        soup = _get_soup(REPERTOAR_URL)
        if not soup:
            return []

        events = []
        for item in soup.select(".amy-movie-item"):
            movie_events = self._parse_movie(item)
            events.extend(movie_events)

        logger.info(f"TUCKWOOD_DONE | total_events={len(events)}")
        return events

    def _parse_movie(self, item) -> list[CinemaEvent]:
        raw_title = _field(item, "title") or _clean(item.select_one(".amy-movie-field-title"))
        if not raw_title:
            return []

        movie_title_serbian, movie_title = _parse_title(raw_title)
        if not movie_title:
            movie_title = movie_title_serbian
            movie_title_serbian = ''

        poster_el = item.select_one(".amy-movie-item-poster img")
        poster_url = poster_el["src"] if poster_el and poster_el.get("src") else ""

        link_el = item.select_one(".amy-movie-item-poster a, .amy-movie-field-title a")
        movie_url = link_el["href"] if link_el and link_el.get("href") else ""

        genre = _field(item, "amy_genre")

        if movie_url:
            if movie_url not in self._desc_cache:
                logger.debug(f"TUCKWOOD_DESC_FETCH | {movie_url}")
                self._desc_cache[movie_url] = _fetch_description(movie_url)
            desc_short, desc_full = self._desc_cache[movie_url]
        else:
            desc_short, desc_full = '', ''

        events = []

        for cell in item.select(".amy-cell"):
            head = cell.select_one(".amy-head")
            if not head:
                continue

            head_divs = head.find_all("div", recursive=False)
            date_text = head_divs[1].get_text(strip=True) if len(head_divs) > 1 else ""
            cell_date = _parse_date(date_text)
            if not cell_date:
                continue

            intro = cell.select_one(".amy-intro-times")
            if not intro:
                continue

            book_el = intro.select_one("a.button")
            booking_url = book_el["href"] if book_el and book_el.get("href") else self.cinema.get('url', '')

            for div in intro.find_all("div", recursive=False):
                text = div.get_text(strip=True)
                m = TIME_RE.search(text)
                if not m:
                    continue

                time_str = m.group()

                try:
                    dt = datetime.fromisoformat(f"{cell_date}T{time_str}:00")
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
                    movie_genre=genre,
                    movie_description_short=desc_short,
                    movie_description_full=desc_full,
                    movie_poster_url=poster_url,
                    showtime=dt,
                    ticket_url=booking_url,
                    source=self.source,
                ))

        return events
