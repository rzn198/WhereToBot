import re
import asyncio
import httpx
from loguru import logger
from telegram import Bot

from db.tables import Tables
from cinema_collector.movie_keys import canonical_key
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


INSERT_SQL = f"""
    INSERT INTO {Tables.CINEMA_EVENTS} (
        cinema_name, cinema_maps_url, cinema_url,
        movie_id, movie_title, movie_title_serbian,
        movie_title_normalized,
        movie_genre, movie_description_short, movie_description_full,
        movie_poster_url, movie_poster_file_id, movie_poster_msg_id,
        showtime, ticket_url, source, movie_key
    ) VALUES (
        $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17
    )
"""

_ALIASES_SQL = f"""
    SELECT alias_key, movie_id FROM {Tables.CINEMA_MOVIE_ALIASES} WHERE alias_key = ANY($1::text[])
"""

_NEW_MOVIE_SQL = f"""
    INSERT INTO {Tables.CINEMA_MOVIES} (title_key, title_serbian, title_original)
    VALUES ($1, $2, $3)
    ON CONFLICT (title_key) DO UPDATE SET last_seen_at = now()
    RETURNING id
"""

_NEW_ALIAS_SQL = f"""
    INSERT INTO {Tables.CINEMA_MOVIE_ALIASES} (alias_key, movie_id) VALUES ($1, $2)
    ON CONFLICT (alias_key) DO NOTHING
"""

# Описание «только растёт»: берём самое длинное из всех кинотеатров, чтобы выбор не прыгал
# между прогонами (иначе AI переводил бы один и тот же фильм заново).
_TOUCH_MOVIE_SQL = f"""
    UPDATE {Tables.CINEMA_MOVIES} SET
        last_seen_at        = now(),
        title_serbian       = COALESCE(title_serbian, $2),
        title_original      = COALESCE(title_original, $3),
        description_src     = CASE WHEN length($4::text) > description_src_len THEN $4::text ELSE description_src END,
        description_src_len = GREATEST(description_src_len, length($4::text)),
        updated_at          = CASE WHEN length($4::text) > description_src_len THEN now() ELSE updated_at END
    WHERE id = $1
"""


def movie_key_of(event: CinemaEvent) -> str:
    """Канонический ключ фильма для сеанса (пусто, если из названия ключ не построить)."""
    return (canonical_key(normalize_title(event.movie_title_serbian or event.movie_title))
            or canonical_key(normalize_title(event.movie_title)))


def _best_description(event: CinemaEvent) -> str:
    return max((event.movie_description_full or '', event.movie_description_short or ''), key=len).strip()


async def _resolve_movies(conn, events: list[CinemaEvent], keys: list[str]) -> dict[str, int]:
    """key -> cinema_movies.id: сначала по алиасам (там же результат склейки AI), нового фильма создаём.

    Одновременно обновляет last_seen_at, недостающие названия и самое длинное описание.
    Вызывается внутри транзакции saver'а, обёрнутой в savepoint: сбой слоя фильмов не должен ломать афишу.
    """
    uniq = sorted({k for k in keys if k})
    if not uniq:
        return {}
    ids = {r['alias_key']: r['movie_id'] for r in await conn.fetch(_ALIASES_SQL, uniq)}

    first: dict[str, CinemaEvent] = {}
    best_desc: dict[str, str] = {}
    for ev, key in zip(events, keys):
        if not key:
            continue
        first.setdefault(key, ev)
        d = _best_description(ev)
        if len(d) > len(best_desc.get(key, '')):
            best_desc[key] = d

    for key in uniq:
        if key in ids:
            continue
        ev = first[key]
        movie_id = await conn.fetchval(_NEW_MOVIE_SQL, key, ev.movie_title_serbian or None, ev.movie_title or None)
        await conn.execute(_NEW_ALIAS_SQL, key, movie_id)
        # алиас мог уже существовать (параллельный saver): берём то, что записано
        ids[key] = await conn.fetchval(
            f"SELECT movie_id FROM {Tables.CINEMA_MOVIE_ALIASES} WHERE alias_key = $1", key)

    await conn.executemany(_TOUCH_MOVIE_SQL, [
        (ids[key], first[key].movie_title_serbian or None, first[key].movie_title or None, best_desc.get(key, ''))
        for key in uniq
    ])
    return ids


async def _poster_for(bot, event, poster_cache):
    """file_id постера в Telegram (скачивание и загрузка — только если передан bot)."""
    if not (bot and event.movie_poster_url):
        return None, None
    if event.movie_poster_url in poster_cache:
        return poster_cache[event.movie_poster_url]
    photo_bytes = await download_poster(event.movie_poster_url)
    if not photo_bytes:
        return None, None
    file_id, msg_id = await upload_poster(bot, photo_bytes, event.movie_title)
    if file_id:
        poster_cache[event.movie_poster_url] = (file_id, msg_id)
    await asyncio.sleep(SEND_DELAY)
    return file_id, msg_id


async def save_events(
    pool,
    events: list[CinemaEvent],
    bot: Bot | None = None,
) -> int:
    """Заменить все сеансы источника на новые.

    Удаление и вставка идут одной транзакцией: бот до COMMIT видит прежнюю афишу целиком,
    а не пустую или недозагруженную. Если вставка не удалась — откат, старые данные целы,
    возвращается 0 (health-монитор сообщит админу).
    """
    if not events:
        return 0

    source = events[0].source

    poster_cache: dict[str, tuple[str, int]] = {}
    rows = []
    for event in events:
        poster_file_id, poster_msg_id = await _poster_for(bot, event, poster_cache)
        serbian = event.movie_title_serbian or event.movie_title
        rows.append((
            event.cinema_name,
            event.cinema_maps_url,
            event.cinema_url,
            event.movie_id,
            event.movie_title,
            event.movie_title_serbian,
            normalize_title(serbian),
            event.movie_genre,
            event.movie_description_short,
            event.movie_description_full,
            event.movie_poster_url,
            poster_file_id,
            poster_msg_id,
            event.showtime,
            event.ticket_url,
            event.source,
        ))

    keys = [movie_key_of(e) for e in events]
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                deleted = await conn.execute(
                    f"DELETE FROM {Tables.CINEMA_EVENTS} WHERE source = $1", source)
                movie_ids: dict[str, int] = {}
                try:
                    async with conn.transaction():      # savepoint: сбой фильмов не откатывает сеансы
                        movie_ids = await _resolve_movies(conn, events, keys)
                except Exception as e:
                    logger.error(f"CINEMA_MOVIES_ERROR | source={source} | {type(e).__name__}: {e} | "
                                 f"сеансы сохраняются без movie_key")
                await conn.executemany(
                    INSERT_SQL, [row + (movie_ids.get(key),) for row, key in zip(rows, keys)])
    except Exception as e:
        logger.error(f"CINEMA_SAVE_ERROR | source={source} | rows={len(rows)} | {type(e).__name__}: {e} | "
                     f"откат, прежние данные сохранены")
        return 0

    logger.info(f"CINEMA_CLEANUP | source={source} | deleted={deleted}")
    logger.info(f"CINEMA_SAVED | source={source} | saved={len(rows)} | movies={len(set(movie_ids.values()))}")
    return len(rows)
