import time
from dataclasses import dataclass, field
from datetime import datetime

import requests
from loguru import logger


def http_get_with_retry(
    url: str,
    headers: dict,
    timeout: int,
    max_retries: int = 2,
    retry_delay: float = 3.0,
) -> requests.Response | None:
    last_error = None
    for attempt in range(max_retries + 1):
        try:
            r = requests.get(url, headers=headers, timeout=timeout)
            r.raise_for_status()
            return r
        except Exception as e:
            last_error = e
            if attempt < max_retries:
                logger.warning(
                    f"HTTP_GET_RETRY | url={url} | attempt={attempt + 1}/{max_retries} | {e}"
                )
                time.sleep(retry_delay)

    logger.error(f"HTTP_GET_FAILED | url={url} | {last_error}")
    return None


@dataclass
class CinemaEvent:
    cinema_name:              str
    cinema_maps_url:          str
    cinema_url:               str
    movie_title:              str
    showtime:                 datetime
    ticket_url:               str
    source:                   str
    movie_id:                 str = ''
    movie_title_serbian:      str = ''
    movie_genre:              str = ''
    movie_description_short:  str = ''
    movie_description_full:   str = ''
    movie_poster_url:         str = ''
    cinema_description:       str = ''


class BaseScraper:

    source: str = ''

    def fetch(self, target_date: str) -> list[CinemaEvent]:
        raise NotImplementedError
