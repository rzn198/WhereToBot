import re
from datetime import datetime, date, timedelta
from loguru import logger
from bs4 import BeautifulSoup
from zoneinfo import ZoneInfo

from cinema_collector.scrapers.base import BaseScraper, CinemaEvent, http_get_with_retry
from shared.constants import MOVIE_DESC_SHORT_COLLECT_LENGTH, MOVIE_DESC_FULL_COLLECT_LENGTH

BELGRADE_TZ = ZoneInfo('Europe/Belgrade')
BASE_URL = "http://www.rodacineplex.com"
TIMEOUT = 15

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "sr-RS,sr;q=0.9,en;q=0.8",
}

TIME_RE = re.compile(r'(\d{1,2}):(\d{2})')


def _get_soup(url: str) -> BeautifulSoup | None:
    r = http_get_with_retry(url, headers=HEADERS, timeout=TIMEOUT)
    if r is None:
        return None
    return BeautifulSoup(r.text, "html.parser")


def _clean(el) -> str:
    return el.get_text(" ", strip=True) if el else ""


def _fetch_available_dates() -> list[str]:
    soup = _get_soup(f"{BASE_URL}/repertoar")
    if not soup:
        return []

    dates = []
    today = date.today()

    for a in soup.select("a[href*='/repertoar/']"):
        href = a.get("href", "")
        m = re.search(r'/repertoar/(\d{4}-\d{2}-\d{2})', href)
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


def _fetch_movie_detail(movie_url: str) -> str:
    if not movie_url:
        return ''

    soup = _get_soup(movie_url)
    if not soup:
        return ''

    sinopsis_el = soup.select_one('.sinopsis')
    if not sinopsis_el:
        return ''

    text = sinopsis_el.get_text(strip=True)
    if text.lower().startswith('sinopsis:'):
        text = text[len('sinopsis:'):].strip()

    return text


class RodaScraper(BaseScraper):
    source = 'roda'

    def __init__(self, cinema: dict):
        self.cinema = cinema
        self._desc_cache: dict[str, str] = {}

    def fetch(self) -> list[CinemaEvent]:
        logger.info("RODA_FETCH | start")

        dates = _fetch_available_dates()
        if not dates:
            logger.warning("RODA_NO_DATES | fallback to 7 days")
            today = date.today()
            dates = [str(today + timedelta(days=i)) for i in range(7)]

        logger.info(f"RODA_DATES | count={len(dates)} | first={dates[0]} | last={dates[-1]}")

        all_events = []
        for date_str in dates:
            events = self._fetch_date(date_str)
            all_events.extend(events)

        logger.info(f"RODA_DONE | total_events={len(all_events)}")
        return all_events

    def _fetch_date(self, date_str: str) -> list[CinemaEvent]:
        url = f"{BASE_URL}/repertoar/{date_str}"
        soup = _get_soup(url)
        if not soup:
            return []

        events = []
        for item in soup.select("li.replist"):
            movie_events = self._parse_movie(item, date_str)
            events.extend(movie_events)

        logger.debug(f"RODA_DATE | date={date_str} | events={len(events)}")
        return events

    def _parse_movie(self, item, date_str: str) -> list[CinemaEvent]:
        col = item.select_one(".col-md-5")
        if not col:
            return []

        h3 = col.select_one("h3")
        movie_title_serbian = _clean(h3) if h3 else ""
        if not movie_title_serbian:
            return []

        movie_title = ""
        a_title = col.select_one("a[href*='/film/']")
        if a_title:
            p = a_title.find("p")
            if p:
                movie_title = _clean(p)

        if not movie_title or movie_title == movie_title_serbian:
            movie_title = movie_title_serbian
            movie_title_serbian = ""

        genre = ""
        for div in item.select(".info_block .clearfix"):
            strong = div.select_one("strong")
            if strong and "Žanr" in _clean(strong):
                full_text = _clean(div)
                genre = full_text.replace("Žanr:", "").strip()
                break

        poster_url = ""
        img = item.select_one("img.img-responsive")
        if img:
            poster_url = img.get("src") or ""
            if poster_url and not poster_url.startswith("http"):
                poster_url = BASE_URL + poster_url

        movie_url = ""
        link_el = item.select_one("a.shop-item-image[href*='/film/']")
        if link_el:
            href = link_el.get("href", "")
            movie_url = href if href.startswith("http") else BASE_URL + href

        if movie_url and movie_url not in self._desc_cache:
            logger.debug(f"RODA_DETAIL_FETCH | {movie_url}")
            self._desc_cache[movie_url] = _fetch_movie_detail(movie_url)

        description_full = self._desc_cache.get(movie_url, '')
        description_short = description_full[:MOVIE_DESC_SHORT_COLLECT_LENGTH] if description_full else ''

        events = []

        vreme_block = item.select_one(".vreme_block")
        if not vreme_block:
            return []

        for perf in vreme_block.select("a"):
            time_el = perf.select_one("h3")
            time_text = _clean(time_el)
            m = TIME_RE.search(time_text)
            if not m:
                continue

            time_str = f"{int(m.group(1)):02d}:{m.group(2)}"

            href = perf.get("href", "")
            if href and not href.startswith("javascript"):
                ticket_url = href if href.startswith("http") else BASE_URL + href
            else:
                ticket_url = self.cinema.get('url', '')

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
                movie_genre=genre,
                movie_description_short=description_short,
                movie_description_full=description_full[:MOVIE_DESC_FULL_COLLECT_LENGTH],
                movie_poster_url=poster_url,
                showtime=dt,
                ticket_url=ticket_url,
                source=self.source,
            ))

        return events
