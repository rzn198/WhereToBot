"""AI киноафиши на настоящей БД с подставным клиентом: названия, склейка, жанры, перевод, «тень», лимиты (нужна DEDUP_TEST_DSN)."""
import json
import os
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest

from cinema_ai import titles as T
from cinema_ai.service import run_pass
from cinema_ai.settings import CinemaAISettings
from cinema_collector.saver import save_events
from cinema_collector.scrapers.base import CinemaEvent
from shared.ai import AIStats
from translator.text import SYSTEM_PROMPT as TRANSLATE_PROMPT

DSN = os.getenv("DEDUP_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="нужна DEDUP_TEST_DSN")
T0 = datetime.now(timezone.utc) + timedelta(days=1)
SR_TEXT = ("Radnja se odvija sedamnaest godina nakon prethodnog poglavlja i vodi gledaoce kroz novu avanturu "
           "pune opasnosti, politike i velikih odluka.")
RU_TEXT = ("Действие происходит спустя семнадцать лет после предыдущей части и ведёт зрителей через новое приключение, "
           "полное опасностей, политики и больших решений.")


class FakeClient:
    configured = True
    models = ['m1']
    current_model = 'm1'

    def __init__(self, titles=None, genres=None, translate=None):
        self.titles = titles or (lambda films: [])
        self.genres = genres or (lambda terms: {})
        self.translate = translate or (lambda text: RU_TEXT)
        self.calls = {'titles': 0, 'genres': 0, 'translate': 0}
        self.stats = AIStats()

    def begin_run(self): pass
    def report_ok(self): pass
    def report_bad_output(self): pass
    async def aclose(self): pass

    async def complete(self, system, user):
        if system == TRANSLATE_PROMPT:
            self.calls['translate'] += 1
            return self.translate(user)
        if system == T.SYSTEM_PROMPT:
            self.calls['titles'] += 1
            ids = [int(l.split(' | ')[0]) for l in user.split('CONTEXT:')[0].split('\n')[1:] if l.strip()]
            return json.dumps({'films': self.titles(ids)}, ensure_ascii=False)
        self.calls['genres'] += 1
        terms = json.loads(user)
        return json.dumps({'labels': self.genres(terms)}, ensure_ascii=False)


@pytest.fixture
async def pool():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    for t in ("cinema_events", "cinema_movie_aliases", "cinema_movies", "cinema_genre_map"):
        await p.execute(f"TRUNCATE {t} RESTART IDENTITY CASCADE")
    yield p
    await p.close()


def ev(source, serbian, original='', desc=SR_TEXT, genre='Drama', hours=0):
    return CinemaEvent(cinema_name='K', cinema_maps_url='', cinema_url='', movie_title=original or serbian,
                       movie_title_serbian=serbian, showtime=T0 + timedelta(hours=hours), ticket_url=f'u/{source}/{serbian}/{hours}',
                       source=source, movie_description_full=desc, movie_genre=genre)


def one_title(ids):
    return [{'id': i, 'en': f'Film {i}', 'ru': f'Фильм {i}', 'same_as': None} for i in ids]


GENRES = lambda terms: {t: {'Drama': 'драма', 'Akcija': 'боевик', 'Filmovi': None}.get(t) for t in terms}   # noqa: E731


async def movies(pool):
    return await pool.fetch("SELECT * FROM cinema_movies ORDER BY id")


async def test_full_pass_titles_genres_translation_and_status(pool):
    await save_events(pool, [ev('cineplexx', 'Dina: Treći Deo', 'Dune: Part Three', genre='Akcija, Drama'),
                             ev('cinestar', 'Klaber', 'Club Kid', genre='Filmovi, Drama', hours=1)])
    c = FakeClient(titles=one_title, genres=GENRES)
    res = await run_pass(pool, c, CinemaAISettings(method='on'))
    assert res.status == 'ok' and res.titles_done == 2 and res.translated == 2
    a, b = await movies(pool)
    assert (a['title_en'], a['title_ru'], a['ai_version']) == (f'Film {a["id"]}', f'Фильм {a["id"]}', T.TITLES_VERSION)
    assert a['genres_ru'] == ['боевик', 'драма'] and b['genres_ru'] == ['драма']
    assert a['description_ru'] == RU_TEXT and a['translated_src_len'] == len(SR_TEXT) and a['status'] == 'done'
    assert await pool.fetchval("SELECT count(*) FROM cinema_genre_map") == 3          # Filmovi сохранён как «не жанр»


async def test_second_pass_makes_no_calls(pool):
    await save_events(pool, [ev('cineplexx', 'Film A')])
    await run_pass(pool, FakeClient(titles=one_title, genres=GENRES), CinemaAISettings())
    c = FakeClient(titles=one_title, genres=GENRES)
    res = await run_pass(pool, c, CinemaAISettings())
    assert c.calls == {'titles': 0, 'genres': 0, 'translate': 0} and res.titles_todo == 0 and res.desc_todo == 0


async def test_shadow_does_not_write_anything(pool):
    await save_events(pool, [ev('cineplexx', 'Film A')])
    before = await movies(pool)
    c = FakeClient(titles=one_title, genres=GENRES)
    res = await run_pass(pool, c, CinemaAISettings(), apply=False)
    assert c.calls['titles'] == 1 and c.calls['translate'] == 1 and {r['kind'] for r in res.collect} == {'title', 'genre', 'desc'}
    after = await movies(pool)
    assert [dict(r) for r in before] == [dict(r) for r in after]
    assert await pool.fetchval("SELECT count(*) FROM cinema_genre_map") == 0


async def test_ai_merge_joins_variants_and_repoints_sessions(pool):
    await save_events(pool, [ev('cineplexx', 'Dina: Treći Deo', 'Dune: Part Three')])
    await save_events(pool, [ev('cinestar', 'Dune Part 3', hours=1, desc=SR_TEXT + ' Dodatak.')])
    a, b = [r['id'] for r in await movies(pool)]

    def titles(ids):
        return [{'id': i, 'en': 'Dune: Part Three', 'ru': 'Дюна: Часть третья', 'same_as': a if i == b else None} for i in ids]
    res = await run_pass(pool, FakeClient(titles=titles, genres=GENRES), CinemaAISettings())
    assert res.merges == 1 and res.merge_vetoes == 0
    ms = await movies(pool)
    assert [r['id'] for r in ms] == [a] and ms[0]['title_ru'] == 'Дюна: Часть третья'
    assert await pool.fetchval("SELECT count(DISTINCT movie_key) FROM cinema_events") == 1
    assert await pool.fetchval("SELECT count(*) FROM cinema_movie_aliases WHERE movie_id=$1", a) == 2
    assert ms[0]['description_src'].endswith('Dodatak.')                                 # длинное описание победителю
    await save_events(pool, [ev('cinestar', 'Dune Part 3', hours=2)])                    # saver дальше идёт по алиасу
    assert [r['id'] for r in await movies(pool)] == [a]


async def test_merge_of_sequels_is_vetoed(pool):
    await save_events(pool, [ev('cineplexx', 'Avatar'), ev('cineplexx', 'Avatar 2', hours=1)])
    a, b = [r['id'] for r in await movies(pool)]
    res = await run_pass(pool, FakeClient(
        titles=lambda ids: [{'id': i, 'en': 'Avatar', 'ru': None, 'same_as': a if i == b else None} for i in ids], genres=GENRES),
        CinemaAISettings())
    assert res.merges == 0 and res.merge_vetoes == 1 and len(await movies(pool)) == 2


async def test_bad_titles_count_fails_then_film_is_skipped(pool):
    await save_events(pool, [ev('cineplexx', 'Film A')])
    bad = lambda ids: [{'id': i, 'en': 'Фильм', 'ru': None, 'same_as': None} for i in ids]   # noqa: E731
    st = CinemaAISettings(max_fails=3)
    for _ in range(3):
        res = await run_pass(pool, FakeClient(titles=bad, genres=GENRES), st)
        assert res.titles_failed == 1
    m = (await movies(pool))[0]
    assert m['ai_titles_fails'] == 3 and m['ai_version'] is None and m['description_ru'] == RU_TEXT
    c = FakeClient(titles=bad, genres=GENRES)
    await run_pass(pool, c, st)
    assert c.calls['titles'] == 0                                                          # токены больше не тратим


async def test_retranslate_only_when_description_grew_enough(pool):
    await save_events(pool, [ev('cineplexx', 'Film A', desc='a' * 400 + ' слово')])
    await run_pass(pool, FakeClient(titles=one_title, genres=GENRES, translate=lambda t: RU_TEXT), CinemaAISettings())
    await save_events(pool, [ev('cineplexx', 'Film A', desc='a' * 400 + ' слово' + ' b' * 60)])          # ~1.3x
    c = FakeClient(titles=one_title, genres=GENRES)
    await run_pass(pool, c, CinemaAISettings())
    assert c.calls['translate'] == 0
    await save_events(pool, [ev('cineplexx', 'Film A', desc='a' * 400 + ' слово' + ' b' * 300)])         # ~2.5x
    c = FakeClient(titles=one_title, genres=GENRES, translate=lambda t: RU_TEXT + ' Новое.')
    await run_pass(pool, c, CinemaAISettings())
    m = (await movies(pool))[0]
    assert c.calls['translate'] == 1 and m['description_ru'].endswith('Новое.') and m['translated_src_len'] > 800


async def test_russian_description_is_copied_without_model(pool):
    await save_events(pool, [ev('cineplexx', 'Film A', desc=RU_TEXT)])
    c = FakeClient(titles=one_title, genres=GENRES)
    res = await run_pass(pool, c, CinemaAISettings())
    assert c.calls['translate'] == 0 and res.copied_ru == 1
    assert (await movies(pool))[0]['description_ru'] == RU_TEXT


async def test_translation_failure_keeps_old_text_and_counts_fail(pool):
    await save_events(pool, [ev('cineplexx', 'Film A')])
    res = await run_pass(pool, FakeClient(titles=one_title, genres=GENRES, translate=lambda t: t), CinemaAISettings())   # вернул оригинал
    m = (await movies(pool))[0]
    assert res.trans_failed == 1 and m['description_ru'] is None and m['ai_desc_fails'] == 1 and m['status'] == 'new'


async def test_description_changed_during_translation_is_not_saved(pool):
    await save_events(pool, [ev('cineplexx', 'Film A')])

    def translate(text):
        import asyncio
        asyncio.get_event_loop().create_task(asyncio.sleep(0))
        return RU_TEXT
    c = FakeClient(titles=one_title, genres=GENRES, translate=translate)
    orig = c.complete

    async def racing(system, user):
        out = await orig(system, user)
        if system == TRANSLATE_PROMPT:
            await pool.execute("UPDATE cinema_movies SET description_src_len = description_src_len + 10")
        return out
    c.complete = racing
    res = await run_pass(pool, c, CinemaAISettings())
    assert res.stale == 1 and (await movies(pool))[0]['description_ru'] is None


async def test_per_run_cap_and_order_by_sessions(pool):
    await save_events(pool, [ev('cineplexx', 'Rare', hours=0)] +
                      [ev('cineplexx', 'Popular', hours=h) for h in range(1, 5)])
    seen = []

    def titles(ids):
        seen.extend(ids)
        return one_title(ids)
    await run_pass(pool, FakeClient(titles=titles, genres=GENRES), CinemaAISettings(max_films_per_run=1))
    popular = await pool.fetchval("SELECT id FROM cinema_movies WHERE title_serbian='Popular'")
    assert seen == [popular]


async def test_unconfigured_client_is_skipped(pool):
    await save_events(pool, [ev('cineplexx', 'Film A')])
    c = FakeClient()
    c.configured = False
    res = await run_pass(pool, c, CinemaAISettings())
    assert res.status == 'skipped'


async def test_films_without_future_sessions_are_ignored(pool):
    old = ev('cineplexx', 'Old')
    old.showtime = datetime.now(timezone.utc) - timedelta(days=2)
    await save_events(pool, [old])
    c = FakeClient(titles=one_title, genres=GENRES)
    res = await run_pass(pool, c, CinemaAISettings())
    assert res.films == 0 and c.calls == {'titles': 0, 'genres': 0, 'translate': 0}


async def test_title_variants_differing_only_by_format_are_collapsed(pool):
    from cinema_ai import store
    await save_events(pool, [ev('tuckwood', 'Hokus: Magično Kraljevstvo (sinhro.) – 2D', 'Hexed'),
                             ev('roda', 'HOKUS: MAGIČNO KRALJEVSTVO 3D (sinhronizovano)', 'Hexed 3D', hours=1),
                             ev('cineplexx', 'Hokus: Magično kraljevstvo', 'Hexed', hours=2)])
    mid = await pool.fetchval("SELECT id FROM cinema_movies")
    v = (await store.load_variants(pool, [mid]))[mid]
    assert len(v) == 2 and any('Hexed' in t for t in v)
