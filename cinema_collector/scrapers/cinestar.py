import re
from datetime import datetime, date
from loguru import logger
from bs4 import BeautifulSoup
from zoneinfo import ZoneInfo

from cinema_collector.scrapers.base import BaseScraper, CinemaEvent, http_get_with_retry
from shared.constants import MOVIE_DESC_SHORT_COLLECT_LENGTH, MOVIE_DESC_FULL_COLLECT_LENGTH

BELGRADE_TZ = ZoneInfo('Europe/Belgrade')
BASE_URL = "https://cinestarcinemas.rs"
TIMEOUT = 15

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "sr-RS,sr;q=0.9,en;q=0.8",
}

TIME_RE = re.compile(r'^([01]?\d|2[0-3]):[0-5]\d$')
DATE_RE = re.compile(r'(\d{1,2})\.(\d{1,2})\.')


def _get_soup(url: str) -> BeautifulSoup | None:
    r = http_get_with_retry(url, headers=HEADERS, timeout=TIMEOUT)
    if r is None:
        return None
    return BeautifulSoup(r.text, "html.parser")


def _clean(el) -> str:
    return el.get_text(" ", strip=True) if el else ""


def _parse_date(day_str: str) -> str | None:
    m = DATE_RE.search(day_str)
    if not m:
        return None
    day = int(m.group(1))
    month = int(m.group(2))
    year = date.today().year
    if month < date.today().month:
        year += 1
    try:
        return f"{year}-{month:02d}-{day:02d}"
    except ValueError:
        return None


def _fetch_movie_detail(movie_url: str) -> dict:
    result = {
        'movie_title': '',
        'movie_genre': '',
        'movie_description_full': '',
    }

    if not movie_url:
        return result

    soup = _get_soup(movie_url)
    if not soup:
        return result

    for detail_item in soup.select('div.movie-detail-item'):
        label = detail_item.select_one('span.gray')
        if label and 'Izvorni' in label.get_text():
            value_span = label.find_next_sibling('span')
            if value_span:
                result['movie_title'] = value_span.get_text(strip=True)
            break

    age_el = soup.select_one('div.age')
    if age_el:
        raw = age_el.get_text(strip=True)
        if 'Žanr:' in raw:
            result['movie_genre'] = raw.split('Žanr:', 1)[1].strip()

    paragraphs = []
    for p in soup.find_all('p'):
        if not p.get('class'):
            text = p.get_text(strip=True)
            if len(text) > 50:
                paragraphs.append(text)
    result['movie_description_full'] = '\n\n'.join(paragraphs)

    return result


class CineStarScraper(BaseScraper):
    source = 'cinestar'

    def __init__(self, cinema: dict):
        self.cinema = cinema
        self.slug = cinema.get('slug', 'beograd-concept-cinema-ada-mall')
        self._detail_cache: dict[str, dict] = {}

    def fetch(self) -> list[CinemaEvent]:
        logger.info(f"CINESTAR_FETCH | slug={self.slug}")

        url = f"{BASE_URL}/{self.slug}"
        soup = _get_soup(url)
        if not soup:
            return []

        events = []

        movie_items = soup.select(".movie-item, .my-movie-item")
        if not movie_items:
            logger.warning(f"CINESTAR_NO_MOVIES | slug={self.slug}")
            return []

        logger.info(f"CINESTAR_MOVIES | count={len(movie_items)}")

        for item in movie_items:
            movie_events = self._parse_movie(item)
            events.extend(movie_events)

        logger.info(f"CINESTAR_DONE | slug={self.slug} | total_events={len(events)}")
        return events

    def _parse_movie(self, item) -> list[CinemaEvent]:
        title_el = item.select_one(".movie-title, h2, h3")
        if not title_el:
            return []

        for label in title_el.select('.filmlabel'):
            label.decompose()
        movie_title_serbian = title_el.get_text(strip=True)

        if not movie_title_serbian:
            return []

        poster_el = item.select_one("img")
        poster_url = ""
        if poster_el:
            poster_url = poster_el.get("src") or poster_el.get("data-src") or ""

        link_el = item.select_one("a[href]")
        movie_url = ""
        if link_el:
            href = link_el.get("href", "")
            if href.startswith("http"):
                movie_url = href
            elif href.startswith("/"):
                movie_url = BASE_URL + href

        if movie_url and movie_url not in self._detail_cache:
            logger.debug(f"CINESTAR_DETAIL_FETCH | {movie_url}")
            self._detail_cache[movie_url] = _fetch_movie_detail(movie_url)

        detail = self._detail_cache.get(movie_url, {})

        movie_title = detail.get('movie_title', '').strip()
        if not movie_title or movie_title == movie_title_serbian:
            movie_title = movie_title_serbian
            movie_title_serbian = ''

        movie_genre = detail.get('movie_genre', '')
        description_full = detail.get('movie_description_full', '')
        description_short = description_full[:MOVIE_DESC_SHORT_COLLECT_LENGTH] if description_full else ''

        events = []

        kinoholder = item.select_one(".kinoholder")
        day_wrappers = kinoholder.select(".day-wrapper") if kinoholder else item.select(".day-wrapper")

        for day_wrapper in day_wrappers:
            day_el = day_wrapper.select_one(".day")
            day_str = _clean(day_el)
            event_date = _parse_date(day_str)
            if not event_date:
                continue

            for perf in day_wrapper.select("a.perf"):
                time_el = perf.select_one(".time")
                time_str = _clean(time_el)
                if not TIME_RE.match(time_str):
                    continue

                href = perf.get("href", "")
                if href and not href.startswith("javascript"):
                    if href.startswith("http"):
                        ticket_url = href
                    elif href.startswith("/"):
                        ticket_url = BASE_URL + href
                    else:
                        ticket_url = movie_url or self.cinema.get('url', '')
                else:
                    ticket_url = movie_url or self.cinema.get('url', '')

                try:
                    dt = datetime.fromisoformat(f"{event_date}T{time_str}:00")
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
