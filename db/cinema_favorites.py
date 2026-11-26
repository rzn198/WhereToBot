"""Избранное киноафиши: конкретный сеанс (фильм + кинотеатр + время). Таблица cinema_favorites (миграция 024).

Сеанс «жив», пока в cinema_events есть строка с тем же фильмом, кинотеатром и временем; исчез из афиши — «снят»
(в списке помечен, в пуш не попадает). Прошедшие сеансы в списке и пуше не показываем, строки старше суток чистит cleanup."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from db.tables import Tables
from shared.tz import BELGRADE_TZ

LIST_LIMIT = 30
PURGE_AFTER = timedelta(days=1)

_ALIVE = f"""EXISTS (SELECT 1 FROM {Tables.CINEMA_EVENTS} e
                    WHERE e.movie_key = f.movie_id AND e.cinema_name = f.cinema_name AND e.showtime = f.showtime)"""
_COLS = """f.id, f.movie_id, f.cinema_name, f.showtime, m.title_serbian, m.title_original, m.title_en, m.title_ru"""


async def add(pool, user_id: int, movie_id: int, cinema_name: str, showtime: datetime) -> None:
    await pool.execute(
        f"""INSERT INTO {Tables.CINEMA_FAVORITES} (user_id, movie_id, cinema_name, showtime)
            VALUES ($1, $2, $3, $4) ON CONFLICT DO NOTHING""", user_id, movie_id, cinema_name, showtime)


async def remove(pool, user_id: int, movie_id: int, cinema_name: str, showtime: datetime) -> None:
    await pool.execute(
        f"""DELETE FROM {Tables.CINEMA_FAVORITES}
            WHERE user_id = $1 AND movie_id = $2 AND cinema_name = $3 AND showtime = $4""",
        user_id, movie_id, cinema_name, showtime)


async def remove_by_id(pool, user_id: int, fav_id: int) -> None:
    await pool.execute(f"DELETE FROM {Tables.CINEMA_FAVORITES} WHERE id = $1 AND user_id = $2", fav_id, user_id)


async def exists(pool, user_id: int, movie_id: int, cinema_name: str, showtime: datetime) -> bool:
    return bool(await pool.fetchval(
        f"""SELECT 1 FROM {Tables.CINEMA_FAVORITES}
            WHERE user_id = $1 AND movie_id = $2 AND cinema_name = $3 AND showtime = $4""",
        user_id, movie_id, cinema_name, showtime))


async def count_upcoming(pool, user_id: int) -> int:
    return int(await pool.fetchval(
        f"SELECT count(*) FROM {Tables.CINEMA_FAVORITES} WHERE user_id = $1 AND showtime >= now()", user_id) or 0)


async def list_upcoming(pool, user_id: int, limit: int = LIST_LIMIT) -> list[dict]:
    """Ближайшие сеансы пользователя по времени; у каждого alive — есть ли он ещё в афише."""
    rows = await pool.fetch(
        f"""SELECT {_COLS}, {_ALIVE} AS alive
            FROM {Tables.CINEMA_FAVORITES} f JOIN {Tables.CINEMA_MOVIES} m ON m.id = f.movie_id
            WHERE f.user_id = $1 AND f.showtime >= now()
            ORDER BY f.showtime, f.id LIMIT $2""", user_id, limit)
    return [dict(r) for r in rows]


async def get(pool, user_id: int, fav_id: int) -> dict | None:
    """Один избранный сеанс со ссылками (из живой строки афиши: карта и билеты)."""
    row = await pool.fetchrow(
        f"""SELECT {_COLS}, {_ALIVE} AS alive,
                   (SELECT e.cinema_maps_url FROM {Tables.CINEMA_EVENTS} e WHERE e.movie_key = f.movie_id
                      AND e.cinema_name = f.cinema_name AND e.showtime = f.showtime ORDER BY e.id LIMIT 1) AS maps_url,
                   (SELECT e.ticket_url FROM {Tables.CINEMA_EVENTS} e WHERE e.movie_key = f.movie_id
                      AND e.cinema_name = f.cinema_name AND e.showtime = f.showtime ORDER BY e.id LIMIT 1) AS ticket_url
            FROM {Tables.CINEMA_FAVORITES} f JOIN {Tables.CINEMA_MOVIES} m ON m.id = f.movie_id
            WHERE f.id = $1 AND f.user_id = $2""", fav_id, user_id)
    return dict(row) if row else None


async def fetch_for_push(pool, day: date, user_id: int | None = None) -> list[dict]:
    """Живые сеансы избранного на день по Белграду для пуша (user_id — только этому пользователю)."""
    start = datetime.combine(day, datetime.min.time(), tzinfo=BELGRADE_TZ)
    rows = await pool.fetch(
        f"""SELECT f.user_id, {_COLS}
            FROM {Tables.CINEMA_FAVORITES} f JOIN {Tables.CINEMA_MOVIES} m ON m.id = f.movie_id
            WHERE f.showtime >= $1 AND f.showtime < $2 AND ($3::bigint IS NULL OR f.user_id = $3) AND {_ALIVE}
            ORDER BY f.user_id, f.showtime, f.id""", start, start + timedelta(days=1), user_id)
    return [dict(r) for r in rows]


async def purge_past(pool) -> int:
    status = await pool.execute(
        f"DELETE FROM {Tables.CINEMA_FAVORITES} WHERE showtime < now() - make_interval(secs => $1)",
        PURGE_AFTER.total_seconds())
    try:
        return int(status.split()[-1])
    except (ValueError, IndexError):
        return 0
