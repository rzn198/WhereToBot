import re
import asyncio
import httpx
from loguru import logger
from telegram import Bot

from db.tables import Tables
from cinema_collector.scrapers.base import CinemaEvent


POSTER_MAX_SIZE = 5 * 1024 * 1024  # 5 MB
SEND_DELAY = 0.5


_SUFFIXES = re.compile(
    r'\b(3d|2d|4d|imax|dolby|atmos|sinhronizovano|sinhron|sinhro|dubbed|'
    r'pretpremijera|pretprodaja|live in 3d)\b',
    re.IGNORECASE
)

_APOSTROPHE = re.compile(r"[''`]", re.UNICODE)

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)

_SPACES = re.compile(r'\s+')


def normalize_title(title: str) -> str:
    if not title:
        return ''

    t = title.lower()
    t = _SUFFIXES.sub('', t)
    t = _APOSTROPHE.sub('', t)   # убираем апостроф без пробела
    t = _PUNCT.sub(' ', t)
    t = _SPACES.sub(' ', t)
    t = t.strip()

    return t


async def download_poster(url: str) -> bytes | None:
    if not url:
        return None
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(url, follow_redirects=True)
            r.raise_for_status()
            if len(r.content) > POSTER_MAX_SIZE:
                logger.warning(f"POSTER_TOO_LARGE | {url} | {len(r.content)} bytes")
                return None
            return r.content
    except Exception as e:
        logger.error(f"POSTER_DOWNLOAD_ERROR | {url} | {e}")
        return None


async def upload_poster(bot: Bot, photo_bytes: bytes, title: str) -> tuple[str, int] | tuple[None, None]:
    from shared.config import config
    storage_channel = config['telegram']['storage_channel']

    try:
        msg = await bot.send_photo(
            chat_id=storage_channel,
            photo=photo_bytes,
            caption=f"🎬 {title}",
        )
        file_id = msg.photo[-1].file_id
        logger.debug(f"POSTER_UPLOADED | title={title[:30]} | file_id={file_id[:20]}...")
        return file_id, msg.message_id
    except Exception as e:
        logger.error(f"POSTER_UPLOAD_ERROR | {title} | {e}")
        return None, None


async def save_events(
    pool,
    events: list[CinemaEvent],
    bot: Bot | None = None,
) -> int:
    if not events:
        return 0

    source = events[0].source

    deleted = await pool.execute(f"""
        DELETE FROM {Tables.CINEMA_EVENTS}
        WHERE source = $1
    """, source)
    logger.info(f"CINEMA_CLEANUP | source={source} | deleted={deleted}")

    poster_cache: dict[str, tuple[str, int]] = {}

    saved = 0
    for event in events:
        poster_file_id = None
        poster_msg_id = None

        if bot and event.movie_poster_url:
            if event.movie_poster_url in poster_cache:
                poster_file_id, poster_msg_id = poster_cache[event.movie_poster_url]
            else:
                photo_bytes = await download_poster(event.movie_poster_url)
                if photo_bytes:
                    poster_file_id, poster_msg_id = await upload_poster(
                        bot, photo_bytes, event.movie_title
                    )
                    if poster_file_id:
                        poster_cache[event.movie_poster_url] = (poster_file_id, poster_msg_id)
                    await asyncio.sleep(SEND_DELAY)

        serbian = event.movie_title_serbian or event.movie_title
        movie_title_normalized = normalize_title(serbian)

        try:
            await pool.execute(f"""
                INSERT INTO {Tables.CINEMA_EVENTS} (
                    cinema_name, cinema_maps_url, cinema_url,
                    movie_id, movie_title, movie_title_serbian,
                    movie_title_normalized,
                    movie_genre, movie_description_short, movie_description_full,
                    movie_poster_url, movie_poster_file_id, movie_poster_msg_id,
                    showtime, ticket_url, source
                ) VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16
                )
            """,
                event.cinema_name,
                event.cinema_maps_url,
                event.cinema_url,
                event.movie_id,
                event.movie_title,
                event.movie_title_serbian,
                movie_title_normalized,
                event.movie_genre,
                event.movie_description_short,
                event.movie_description_full,
                event.movie_poster_url,
                poster_file_id,
                poster_msg_id,
                event.showtime,
                event.ticket_url,
                event.source,
            )
            saved += 1
        except Exception as e:
            logger.error(f"CINEMA_SAVE_ERROR | {event.movie_title} | {e}")

    logger.info(f"CINEMA_SAVED | source={source} | saved={saved}")
    return saved
