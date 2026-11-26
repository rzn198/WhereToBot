"""БД-слой AI киноафиши: выборки и сохранение результатов. Состояние не хранится в памяти: «что делать» выводится из БД."""
from __future__ import annotations

from loguru import logger

from cinema_ai.genres import split_terms
from cinema_collector.saver import normalize_title
from db.tables import Tables

SOURCE_PRIORITY = ('cineplexx', 'cinestar', 'cinegrand', 'mts_dvorana', 'tuckwood', 'roda')

ACTIVE_SQL = f"""
    SELECT m.id, m.title_key, m.title_serbian, m.title_original, m.title_en, m.title_ru, m.genres_ru,
           m.description_src, m.description_src_len, m.description_ru, m.translated_src_len,
           m.status, m.ai_version, m.ai_titles_fails, m.ai_desc_fails,
           (SELECT count(*) FROM {Tables.CINEMA_EVENTS} e WHERE e.movie_key = m.id AND e.showtime > now()) AS sessions
    FROM {Tables.CINEMA_MOVIES} m
    WHERE EXISTS (SELECT 1 FROM {Tables.CINEMA_EVENTS} e WHERE e.movie_key = m.id AND e.showtime > now())
    ORDER BY sessions DESC, m.id
"""


async def load_active(pool) -> list[dict]:
    """Фильмы, у которых есть будущие сеансы."""
    return [dict(r) for r in await pool.fetch(ACTIVE_SQL)]


async def load_variants(pool, ids: list[int]) -> dict[int, list[str]]:
    """Варианты названий фильма из сеансов (сербские первыми), до 4. Варианты, отличающиеся только форматом
    («… (sinhro.) – 2D» и «… 3D (sinhronizovano)»), регистром или знаками, считаются одним — в модель идут только разные."""
    rows = await pool.fetch(
        f"SELECT movie_key, movie_title_serbian AS sr, movie_title AS orig, count(*) AS n "
        f"FROM {Tables.CINEMA_EVENTS} WHERE movie_key = ANY($1::bigint[]) "
        f"GROUP BY movie_key, movie_title_serbian, movie_title ORDER BY movie_key, n DESC", ids)
    out: dict[int, list[str]] = {i: [] for i in ids}
    seen: dict[int, set[str]] = {i: set() for i in ids}
    for pass_field in ('sr', 'orig'):
        for r in rows:
            t = ' '.join((r[pass_field] or '').split())
            k = r['movie_key']
            sig = normalize_title(t) or t.casefold()
            if t and sig not in seen[k] and len(out[k]) < 4:
                seen[k].add(sig)
                out[k].append(t)
    return out


async def load_genre_terms(pool, ids: list[int]) -> dict[int, list[tuple[str, str | None]]]:
    rows = await pool.fetch(
        f"SELECT DISTINCT movie_key, source, movie_genre FROM {Tables.CINEMA_EVENTS} "
        f"WHERE movie_key = ANY($1::bigint[]) AND movie_genre IS NOT NULL AND movie_genre <> ''", ids)
    out: dict[int, list[tuple[str, str | None]]] = {i: [] for i in ids}
    for r in rows:
        out[r['movie_key']].append((r['source'], r['movie_genre']))
    return out


async def load_genre_map(pool) -> dict[str, str | None]:
    return {r['term']: r['genre_ru'] for r in await pool.fetch(f"SELECT term, genre_ru FROM {Tables.CINEMA_GENRE_MAP}")}


async def save_genre_map(pool, mapping: dict[str, str | None]) -> None:
    if not mapping:
        return
    await pool.executemany(
        f"INSERT INTO {Tables.CINEMA_GENRE_MAP} (term, genre_ru) VALUES ($1, $2) "
        f"ON CONFLICT (term) DO UPDATE SET genre_ru = EXCLUDED.genre_ru, updated_at = now()",
        list(mapping.items()))


async def save_titles(pool, film_id: int, en: str, ru: str | None, version: str, model: str | None) -> None:
    await pool.execute(
        f"UPDATE {Tables.CINEMA_MOVIES} SET title_en = $2, title_ru = $3, ai_version = $4, ai_model = $5, "
        f"ai_titles_fails = 0, updated_at = now() WHERE id = $1", film_id, en, ru, version, model)


async def bump_fail(pool, ids: list[int], stage: str) -> None:
    """stage: 'titles' | 'desc' — у каждого этапа свой счётчик, успех одного не сбрасывает неудачи другого."""
    column = {'titles': 'ai_titles_fails', 'desc': 'ai_desc_fails'}[stage]
    if ids:
        await pool.execute(f"UPDATE {Tables.CINEMA_MOVIES} SET {column} = {column} + 1 WHERE id = ANY($1::bigint[])", ids)


async def save_description(pool, film_id: int, text: str, src_len: int, model: str | None) -> bool:
    """Перевод сохраняется, только если исходное описание не изменилось за время запроса."""
    res = await pool.execute(
        f"UPDATE {Tables.CINEMA_MOVIES} SET description_ru = $2, translated_src_len = $3, ai_model = $4, "
        f"ai_desc_fails = 0, updated_at = now() WHERE id = $1 AND description_src_len = $3", film_id, text, src_len, model)
    return not res.endswith(' 0')


async def merge_movies(pool, a: int, b: int) -> tuple[int, int] | None:
    """Склеить два фильма. Побеждает тот, у кого уже есть ответ AI, иначе меньший id. Возвращает (победитель, проигравший)."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            rows = {r['id']: r for r in await conn.fetch(
                f"SELECT id, ai_version FROM {Tables.CINEMA_MOVIES} WHERE id = ANY($1::bigint[]) FOR UPDATE", [a, b])}
            if len(rows) != 2:
                return None
            winner, loser = sorted((a, b), key=lambda i: (rows[i]['ai_version'] is None, i))
            await conn.execute(f"UPDATE {Tables.CINEMA_MOVIE_ALIASES} SET movie_id = $1 WHERE movie_id = $2", winner, loser)
            await conn.execute(f"UPDATE {Tables.CINEMA_EVENTS} SET movie_key = $1 WHERE movie_key = $2", winner, loser)
            await conn.execute(
                f"UPDATE {Tables.CINEMA_MOVIES} w SET "
                f"description_src = CASE WHEN l.description_src_len > w.description_src_len THEN l.description_src ELSE w.description_src END, "
                f"description_src_len = GREATEST(w.description_src_len, l.description_src_len), "
                f"title_serbian = COALESCE(w.title_serbian, l.title_serbian), "
                f"title_original = COALESCE(w.title_original, l.title_original), updated_at = now() "
                f"FROM {Tables.CINEMA_MOVIES} l WHERE w.id = $1 AND l.id = $2", winner, loser)
            await conn.execute(f"DELETE FROM {Tables.CINEMA_MOVIES} WHERE id = $1", loser)
    logger.info(f"CINEMA_AI_MERGE | winner={winner} | loser={loser}")
    return winner, loser


async def refresh_status_and_genres(pool, genres_by_film: dict[int, list[str]], max_fails: int) -> None:
    """genres_ru фильмов (только если изменились) и статус: done / failed / new."""
    for fid, genres in genres_by_film.items():
        await pool.execute(
            f"UPDATE {Tables.CINEMA_MOVIES} SET genres_ru = $2, updated_at = now() "
            f"WHERE id = $1 AND genres_ru IS DISTINCT FROM $2", fid, genres or None)
    await pool.execute(
        f"""UPDATE {Tables.CINEMA_MOVIES} SET status = CASE
                WHEN ai_version IS NOT NULL AND (description_src_len = 0 OR description_ru IS NOT NULL) THEN 'done'
                WHEN ai_titles_fails >= $1 OR ai_desc_fails >= $1 THEN 'failed'
                ELSE 'new' END
            WHERE id = ANY($2::bigint[]) """,
        max_fails, list(genres_by_film))


def terms_of(rows: list[tuple[str, str | None]]) -> list[str]:
    out: list[str] = []
    for _s, raw in rows:
        for t in split_terms(raw):
            if t not in out:
                out.append(t)
    return out
