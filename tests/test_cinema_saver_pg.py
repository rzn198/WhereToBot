"""Saver киноафиши с БД: фильмы, алиасы, movie_key, самое длинное описание, устойчивость к сбою (нужна DEDUP_TEST_DSN)."""
import os
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest

from cinema_collector import saver
from cinema_collector.saver import save_events
from cinema_collector.scrapers.base import CinemaEvent

DSN = os.getenv("DEDUP_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="нужна DEDUP_TEST_DSN")
T0 = datetime.now(timezone.utc) + timedelta(days=1)


@pytest.fixture
async def pool():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
    for t in ("cinema_events", "cinema_movie_aliases", "cinema_movies", "cinema_genre_map"):
        await p.execute(f"TRUNCATE {t} RESTART IDENTITY CASCADE")
    yield p
    await p.close()


def ev(source, serbian, original='', desc='', hours=0, cinema='K'):
    return CinemaEvent(
        cinema_name=cinema, cinema_maps_url='', cinema_url='', movie_title=original or serbian,
        movie_title_serbian=serbian, showtime=T0 + timedelta(hours=hours), ticket_url=f'u/{source}/{hours}',
        source=source, movie_description_full=desc, movie_genre='Drama')


async def movies(pool):
    return await pool.fetch("SELECT * FROM cinema_movies ORDER BY id")


async def test_two_sources_with_different_spelling_share_one_movie(pool):
    await save_events(pool, [ev('cineplexx', '3 nedelje posle', 'Three weeks after', desc='kratko')])
    await save_events(pool, [ev('cinestar', 'Tri nedelje posle', desc='ovo je mnogo duže od prvog opisa')])
    ms = await movies(pool)
    assert len(ms) == 1
    assert ms[0]['title_key'] == 'tri nedelje posle'
    assert ms[0]['description_src'] == 'ovo je mnogo duže od prvog opisa'
    keys = await pool.fetch("SELECT DISTINCT movie_key FROM cinema_events")
    assert [r['movie_key'] for r in keys] == [ms[0]['id']]


async def test_different_parts_stay_different_movies(pool):
    await save_events(pool, [ev('cineplexx', 'Dina: Drugi deo'), ev('cineplexx', 'Dina: Treći deo', hours=1)])
    assert len(await movies(pool)) == 2


async def test_resave_keeps_movie_id_and_ai_fields_and_replaces_sessions(pool):
    await save_events(pool, [ev('cineplexx', 'Klaber', 'Club Kid', desc='opis')])
    mid = (await movies(pool))[0]['id']
    await pool.execute("UPDATE cinema_movies SET title_en='Club Kid', title_ru='Клуб Кид', status='done' WHERE id=$1", mid)
    await save_events(pool, [ev('cineplexx', 'Klaber', 'Club Kid', desc='opis', hours=5)])
    ms = await movies(pool)
    assert len(ms) == 1 and ms[0]['id'] == mid and ms[0]['title_ru'] == 'Клуб Кид' and ms[0]['status'] == 'done'
    assert await pool.fetchval("SELECT count(*) FROM cinema_events") == 1          # сеансы пересобраны, не задвоены
    assert await pool.fetchval("SELECT movie_key FROM cinema_events") == mid


async def test_description_only_grows_never_shrinks(pool):
    await save_events(pool, [ev('cineplexx', 'Film', desc='x' * 300)])
    await save_events(pool, [ev('cineplexx', 'Film', desc='короткий', hours=1)])
    m = (await movies(pool))[0]
    assert m['description_src_len'] == 300 and m['description_src'] == 'x' * 300
    await save_events(pool, [ev('cineplexx', 'Film', desc='y' * 400, hours=2)])
    m = (await movies(pool))[0]
    assert m['description_src_len'] == 400


async def test_short_description_used_when_full_empty(pool):
    e = ev('cineplexx', 'Film')
    e.movie_description_short = 'только краткое'
    await save_events(pool, [e])
    assert (await movies(pool))[0]['description_src'] == 'только краткое'


async def test_alias_repoint_by_ai_merge_is_respected_by_next_save(pool):
    await save_events(pool, [ev('cineplexx', 'Dina: Drugi deo')])
    await save_events(pool, [ev('cinestar', 'Dina: Drugi deo Re-release x')])         # другой ключ — пока отдельный фильм
    a, b = [r['id'] for r in await movies(pool)]
    # AI склеил b в a: алиас переносится, фильм-проигравший удаляется
    await pool.execute("UPDATE cinema_movie_aliases SET movie_id=$1 WHERE movie_id=$2", a, b)
    await pool.execute("DELETE FROM cinema_movies WHERE id=$1", b)
    await save_events(pool, [ev('cinestar', 'Dina: Drugi deo Re-release x', hours=3)])
    assert [r['id'] for r in await movies(pool)] == [a]
    assert await pool.fetchval("SELECT movie_key FROM cinema_events WHERE source='cinestar'") == a


async def test_movies_layer_failure_does_not_break_sessions(pool, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError('boom')
    monkeypatch.setattr(saver, '_resolve_movies', boom)
    saved = await save_events(pool, [ev('cineplexx', 'Film')])
    assert saved == 1
    assert await pool.fetchval("SELECT movie_key FROM cinema_events") is None
    assert await pool.fetchval("SELECT count(*) FROM cinema_movies") == 0


async def test_sessions_of_other_sources_untouched(pool):
    await save_events(pool, [ev('cineplexx', 'Film A')])
    await save_events(pool, [ev('cinestar', 'Film B')])
    await save_events(pool, [ev('cineplexx', 'Film A', hours=2)])
    assert await pool.fetchval("SELECT count(*) FROM cinema_events WHERE source='cinestar'") == 1
