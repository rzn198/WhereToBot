"""Хуки строк и обогащение: проверки кодом, клиенты с подменённой сетью, второй проход, строка в боте, копирование по хэшу."""
import json
import os
from datetime import date, time, timedelta

import asyncpg
import httpx
import pytest

from bot.utils import window as w
from card_lines.enrich import DbCache, EnrichUnavailable, Enricher, MemoryCache, artist_names
from card_lines.hooks import BadHook, check_hook, check_time
from card_lines.profiles import build_profiles
from card_lines.service import run_pass
from card_lines.settings import card_line_settings, hook_settings
from card_lines.text import build_user_prompt, parse_single, src_hash, useful_chars, Candidate
from shared.ai import AIClient

DSN = os.getenv("DEDUP_TEST_DSN")
needs_pg = pytest.mark.skipif(not DSN, reason="нужна DEDUP_TEST_DSN")
TODAY = date.today() + timedelta(days=2)
CONCERT = build_profiles(None)['Концерты']
F = CONCERT.formulas
TEXT = "Dubioza kolektiv. Prvi put u Srbiji nastupa bend iz Bosne, koncert povodom 20 godina karijere. Ulaz 1000 din."


def chk(obj, text=TEXT, facts='', title='Dubioza kolektiv'):
    return check_hook(obj, title=title, formulas=F, hook_max=80, text=text, facts=facts)


# ------------------------------------------------------------------ проверки хука

def test_hook_accepted_with_verbatim_evidence_and_number():
    h = chk({'hook': 'Dubioza kolektiv — 20 лет на сцене', 'formula': 1, 'evidence': 'povodom 20 godina karijere'})
    assert h.text == '20 лет на сцене' and h.source == 'text' and h.formula == 1


def test_hook_none_when_model_gives_none():
    assert chk({'hook': None}) is None and chk({'hook': ' '}) is None


@pytest.mark.parametrize('obj', [
    {'hook': 'ок', 'formula': 1, 'evidence': 'povodom 20 godina karijere'},                       # слишком короткий
    {'hook': 'x' * 90, 'formula': 1, 'evidence': 'povodom 20 godina karijere'},                   # длиннее hook_max
    {'hook': 'смотри t.me/abc', 'formula': 1, 'evidence': 'povodom 20 godina karijere'},          # ссылка
    {'hook': 'юбилей группы', 'formula': 9, 'evidence': 'povodom 20 godina karijere'},            # нет такой формулы
    {'hook': 'юбилей группы', 'formula': 1, 'evidence': 'nema ovoga u tekstu nigde'},             # цитаты нет в тексте
    {'hook': 'юбилей группы', 'formula': 1, 'evidence': 'kratko'},                                # цитата слишком короткая
    {'hook': '25 лет на сцене', 'formula': 1, 'evidence': 'povodom 20 godina karijere'},          # число не из цитаты
    {'hook': 'культовая группа Балкан', 'formula': 1, 'evidence': 'povodom 20 godina karijere'},  # громкое слово без доказательства
    {'hook': 'играют rocksteady', 'formula': 1, 'evidence': 'povodom 20 godina karijere'},        # латиница не из источника
    {'hook': 5, 'formula': 1, 'evidence': 'povodom 20 godina karijere'},
])
def test_hook_rejected(obj):
    with pytest.raises(BadHook):
        chk(obj)


def test_loud_word_ok_when_proved_by_quote():
    h = chk({'hook': 'впервые в Сербии', 'formula': 1, 'evidence': 'Prvi put u Srbiji nastupa bend'})
    assert h.text == 'впервые в Сербии'


def test_web_evidence_only_for_web_formula():
    facts = 'MusicBrainz: Dubioza kolektiv; type: Group; origin: Bosnia and Herzegovina; tags: rock, ska'
    ok = chk({'hook': 'боснийская группа, рок и ска', 'formula': 5, 'evidence': 'origin: Bosnia and Herzegovina'},
             text='Dubioza kolektiv', facts=facts)
    assert ok.source == 'web'
    with pytest.raises(BadHook):          # формула 1 («повод») по интернету не разрешена
        chk({'hook': 'боснийская группа', 'formula': 1, 'evidence': 'origin: Bosnia and Herzegovina'}, text='Dubioza kolektiv', facts=facts)


def test_check_time_requires_quote_and_explicit_time():
    t = 'Početak u 21:30 u klubu'
    assert check_time('21:30', 'Početak u 21:30', t) == time(21, 30)
    assert check_time('21:30', 'nema ga', t) is None
    assert check_time('21:00', '21 ноября', 'Концерт 21 ноября') is None       # дата, а не время
    assert check_time('21:00', 'od 21h', 'koncert od 21h') == time(21, 0)
    assert check_time('00:00', 'u 00:00', 'u 00:00') is None
    assert check_time('25:00', 'u 25:00', 'u 25:00') is None


# ------------------------------------------------------------------ промпты и разбор

def cand(title='Dubioza kolektiv', body=TEXT, id=1):
    return Candidate('e', id, title, body, '', CONCERT)


HOOKS_ON = card_line_settings({'method': 'on', 'hooks': {'method': 'on'}}).hooks


def test_prompt_contains_formulas_only_when_hooks_on_and_facts_as_data():
    off = build_user_prompt(cand(), 600, 40)
    on = build_user_prompt(cand(), 600, 40, HOOKS_ON)
    assert 'evidence' not in off and 'evidence' in on
    with_facts = build_user_prompt(cand(), 600, 40, HOOKS_ON, 'origin: Bosnia', 'MusicBrainz')
    assert 'ФАКТЫ ИЗ ИНТЕРНЕТА' in with_facts and 'origin: Bosnia' in with_facts


def test_parse_single_bad_hook_is_soft():
    raw = json.dumps({'title': 'Dubioza kolektiv', 'value': 'рок', 'hook': 'юбилей', 'formula': 1, 'evidence': 'нет такого'})
    p = parse_single(raw, cand(), 600, 40, HOOKS_ON)
    assert p.title == 'Dubioza kolektiv' and p.value == 'рок' and p.hook is None and p.hook_status == 'invalid'


def test_parse_single_good_hook_and_time():
    raw = json.dumps({'title': 'Dubioza kolektiv', 'value': 'рок', 'hook': 'впервые в Сербии', 'formula': 1,
                      'evidence': 'Prvi put u Srbiji nastupa bend', 'enough': True, 'time': '21:00', 'time_evidence': 'ne postoji'})
    p = parse_single(raw, cand(), 600, 40, HOOKS_ON)
    assert p.hook.text == 'впервые в Сербии' and p.text_time is None


def test_useful_chars_ignores_organizer_lines():
    assert useful_chars('ORGANIZATOR/IZVOR: Tickets.rs\n  ') == 0
    assert useful_chars('ORGANIZATOR/IZVOR: X\nKoncert benda u klubu') > 10


def test_src_hash_changes_with_hook_key():
    c = cand()
    assert src_hash(c, 600, 40, '') != src_hash(c, 600, 40, 'h80:1:abc')


def test_hook_settings_defaults_and_flags():
    d = hook_settings(None)
    assert not d.enabled and not d.enrich.enabled and d.hook_max == 80
    s = hook_settings({'method': 'on', 'hook_max': 60, 'enrich': {'method': 'on', 'contact': ' a@b.c '}})
    assert s.enabled and s.hook_max == 60 and s.enrich.enabled and s.enrich.contact == 'a@b.c'
    assert not card_line_settings({'method': 'shadow', 'hooks': {'method': 'on'}}).use_hooks


# ------------------------------------------------------------------ клиенты обогащения

def test_artist_names_split():
    assert artist_names('ACO PEJOVIĆ & EDITA ARADINOVIĆ') == ['ACO PEJOVIĆ', 'EDITA ARADINOVIĆ']
    assert artist_names('Zoster (live)') == ['Zoster']


def enricher(handler, **cfg):
    st = hook_settings({'method': 'on', 'enrich': {'method': 'on', 'contact': 'x@y.z', **cfg}}).enrich
    async def nosleep(_): return None
    return Enricher(st, MemoryCache(), transport=httpx.MockTransport(handler), sleep=nosleep)


def mb(artists):
    return httpx.Response(200, json={'artists': artists})


def wp(pages, redirects=None):
    return httpx.Response(200, json={'query': {'pages': pages, 'redirects': redirects or []}})


async def test_musicbrainz_exact_unique_found_with_facts_and_user_agent():
    seen = []

    def h(req):
        seen.append(req)
        if 'musicbrainz' in req.url.host:
            return mb([{'id': 'abc', 'name': 'Dubioza Kolektiv', 'score': 100, 'type': 'Group', 'country': 'BA',
                        'area': {'name': 'Bosnia and Herzegovina'}, 'life-span': {'begin': '2003'}, 'tags': [{'name': 'rock', 'count': 3}]}])
        return wp([{'missing': True}])

    e = enricher(h)
    f = await e.facts_for('artist', 'DUBIOZA KOLEKTIV')
    assert f and 'Group' in f.text and '2003' in f.text and 'rock' in f.text and f.url.endswith('/abc')
    assert 'x@y.z' in seen[0].headers['user-agent']
    await e.facts_for('artist', 'Dubioza kolektiv')              # из кэша
    assert e.stats['cache_hits'] >= 1 and e.stats['lookups'] == 4


async def test_musicbrainz_inexact_or_low_score_not_used():
    def h(req):
        if 'musicbrainz' in req.url.host:
            return mb([{'id': '1', 'name': 'Zoster Band', 'score': 100}, {'id': '2', 'name': 'Zoster', 'score': 60}])
        return wp([{'missing': True}])
    assert await enricher(h).facts_for('artist', 'Zoster') is None


async def test_musicbrainz_ambiguous_needs_hint():
    arts = [{'id': '1', 'name': 'Smak', 'score': 100, 'country': 'RS', 'area': {'name': 'Serbia'}, 'disambiguation': 'serbian rock band'},
            {'id': '2', 'name': 'Smak', 'score': 100, 'country': 'DE', 'area': {'name': 'Germany'}, 'disambiguation': 'german techno'}]

    def h(req):
        return mb(arts) if 'musicbrainz' in req.url.host else wp([{'missing': True}])
    e = enricher(h)
    assert await e.facts_for('artist', 'Smak', hint='nema podataka') is None
    e2 = enricher(h)
    assert (await e2.facts_for('artist', 'Smak', hint='serbian rock band, Beograd')).url.endswith('/1')


async def test_wikipedia_exact_musician_ok_disambiguation_and_nonmusician_skipped():
    page = {'title': 'Zoster', 'description': 'Serbian rock band', 'extract': 'Zoster is a Serbian rock band from Belgrade.'}

    def h(req):
        if 'musicbrainz' in req.url.host:
            return mb([])
        return wp([page]) if req.url.host.startswith('ru') else wp([{'missing': True}])
    f = await enricher(h).facts_for('artist', 'Zoster')
    assert f and 'Wikipedia (ru)' in f.source and 'rock band' in f.text

    def dis(req):
        return mb([]) if 'musicbrainz' in req.url.host else wp([{**page, 'pageprops': {'disambiguation': ''}}])
    assert await enricher(dis).facts_for('artist', 'Zoster') is None

    def other(req):
        return mb([]) if 'musicbrainz' in req.url.host else wp([{'title': 'Zoster', 'description': 'genus of fungi', 'extract': 'A genus of plants.'}])
    assert await enricher(other).facts_for('artist', 'Zoster') is None

    def wrong_title(req):
        return mb([]) if 'musicbrainz' in req.url.host else wp([{**page, 'title': 'Zoster Pro'}])
    assert await enricher(wrong_title).facts_for('artist', 'Zoster') is None


async def test_network_failure_raises_and_is_not_cached():
    calls = []

    def h(req):
        calls.append(1)
        return httpx.Response(503)
    e = enricher(h)
    with pytest.raises(EnrichUnavailable):
        await e.facts_for('artist', 'Zoster')
    n = len(calls)
    with pytest.raises(EnrichUnavailable):
        await e.facts_for('artist', 'Zoster')
    assert len(calls) > n and not e.cache.data


async def test_partial_failure_still_returns_found_source():
    def h(req):
        if 'musicbrainz' in req.url.host:
            return httpx.Response(500)
        return wp([{'title': 'Zoster', 'description': 'rock band', 'extract': 'Zoster is a rock band.'}])
    assert (await enricher(h).facts_for('artist', 'Zoster')) is not None


async def test_other_kind_returns_none():
    assert await enricher(lambda r: httpx.Response(500)).facts_for('film', 'X') is None


# ------------------------------------------------------------------ проход с хуками (Postgres)

def make_client(handler):
    return AIClient('card-lines', 'k', ['m1'], transport=httpx.MockTransport(handler), retry_delay=0)


def reply(obj):
    return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(obj)}}], 'usage': {'prompt_tokens': 5, 'completion_tokens': 2}})


@pytest.fixture
async def pool():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    for t in ('dedup_decisions', 'dedup_dates', 'dedup_runs', 'card_lines', 'card_enrichment', 'messages', 'events'):
        await p.execute(f"TRUNCATE {t} RESTART IDENTITY CASCADE")
    yield p
    await p.close()


async def add_event(pool, n, title, desc, day=None):
    return await pool.fetchval(
        "INSERT INTO events (source, source_url, title, description, starts_at, city, event_types) "
        "VALUES ('polako', $1, $2, $3, $4, 'Belgrade', ARRAY['Концерты']) RETURNING id", f'https://x.rs/{n}', title, desc, day or TODAY)


ST = card_line_settings({'method': 'on', 'after_dedup': False, 'concurrency': 1,
                         'hooks': {'method': 'on', 'enrich': {'method': 'on', 'contact': 'x@y.z'}}})
OFF = card_line_settings({'method': 'on', 'after_dedup': False, 'concurrency': 1})


async def row(pool, eid):
    return await pool.fetchrow("SELECT * FROM card_lines WHERE kind='e' AND ref_id=$1", eid)


@needs_pg
async def test_pass_a_accepts_hook_from_description_without_internet(pool):
    eid = await add_event(pool, 1, 'DUBIOZA KOLEKTIV', TEXT)
    h = lambda r: reply({'title': 'Dubioza kolektiv', 'value': 'рок', 'hook': 'впервые в Сербии', 'formula': 1,
                         'evidence': 'Prvi put u Srbiji nastupa bend', 'enough': True})
    res = await run_pass(pool, make_client(h), ST, city='Belgrade', horizon_days=30, today=TODAY)
    r = await row(pool, eid)
    assert res.hooks['ok'] == 1 and r['hook'] == 'впервые в Сербии' and r['hook_status'] == 'ok' and r['hook_formula'] == 1
    assert r['hook_source'] == 'text' and r['hook_url'] is None


@needs_pg
async def test_short_description_goes_to_internet_and_second_call_uses_facts(pool):
    eid = await add_event(pool, 1, 'ZOSTER', 'ORGANIZATOR/IZVOR: Tickets')
    prompts = []

    def ai(req):
        body = req.content.decode('utf-8')
        prompts.append(body)
        if 'ФАКТЫ ИЗ ИНТЕРНЕТА' in body:
            return reply({'title': 'Zoster', 'value': 'рок', 'hook': 'сербская рок-группа из Белграда', 'formula': 5,
                          'evidence': 'Zoster is a Serbian rock band from Belgrade'})
        return reply({'title': 'Zoster', 'value': 'рок', 'hook': None, 'enough': False})

    def net(req):
        if 'musicbrainz' in req.url.host:
            return httpx.Response(200, json={'artists': []})
        return httpx.Response(200, json={'query': {'pages': [{'title': 'Zoster', 'description': 'Serbian rock band',
                                                                 'extract': 'Zoster is a Serbian rock band from Belgrade.'}], 'redirects': []}})
    enr = enricher(net)
    res = await run_pass(pool, make_client(ai), ST, city='Belgrade', horizon_days=30, today=TODAY, enricher=enr)
    r = await row(pool, eid)
    assert len(prompts) == 2 and res.calls == 2 and res.hooks['ok_web'] == 1
    assert r['hook_source'] == 'web' and r['hook_status'] == 'ok' and 'wikipedia.org' in r['hook_url']


@needs_pg
async def test_nothing_found_gives_not_found_without_second_call(pool):
    eid = await add_event(pool, 1, 'ZOSTER', 'ORGANIZATOR/IZVOR: Tickets')
    n = []

    def ai(req):
        n.append(1)
        return reply({'title': 'Zoster', 'value': 'рок', 'hook': None})
    enr = enricher(lambda r: httpx.Response(200, json={'artists': []}) if 'musicbrainz' in r.url.host else wp([{'missing': True}]))
    res = await run_pass(pool, make_client(ai), ST, city='Belgrade', horizon_days=30, today=TODAY, enricher=enr)
    r = await row(pool, eid)
    assert len(n) == 1 and r['hook_status'] == 'not_found' and r['hook'] is None and res.hooks['not_found'] == 1
    assert r['short_title'] == 'Zoster'


@needs_pg
async def test_network_failure_marks_retry_and_retries_after_hours(pool):
    eid = await add_event(pool, 1, 'ZOSTER', 'ORGANIZATOR/IZVOR: Tickets')
    ai = lambda r: reply({'title': 'Zoster', 'value': 'рок', 'hook': None})
    enr = enricher(lambda r: httpx.Response(503))
    await run_pass(pool, make_client(ai), ST, city='Belgrade', horizon_days=30, today=TODAY, enricher=enr)
    assert (await row(pool, eid))['hook_status'] == 'retry'
    again = await run_pass(pool, make_client(ai), ST, city='Belgrade', horizon_days=30, today=TODAY, enricher=enr)
    assert again.candidates == 0                                       # рано: ждём retry_hours
    await pool.execute("UPDATE card_lines SET updated_at = now() - interval '7 hours'")
    again = await run_pass(pool, make_client(ai), ST, city='Belgrade', horizon_days=30, today=TODAY, enricher=enr)
    assert again.candidates == 1


@needs_pg
async def test_invalid_hook_keeps_line_and_status_invalid(pool):
    eid = await add_event(pool, 1, 'DUBIOZA KOLEKTIV', TEXT)
    ai = lambda r: reply({'title': 'Dubioza kolektiv', 'value': 'рок', 'hook': 'легендарная группа', 'formula': 1, 'evidence': 'выдумка'})
    await run_pass(pool, make_client(ai), ST, city='Belgrade', horizon_days=30, today=TODAY)
    r = await row(pool, eid)
    assert r['hook_status'] == 'invalid' and r['hook'] is None and r['short_title'] == 'Dubioza kolektiv'


@needs_pg
async def test_same_text_on_other_dates_copied_without_model_call(pool):
    a = await add_event(pool, 1, 'DUBIOZA KOLEKTIV', TEXT)
    calls = []

    def ai(req):
        calls.append(1)
        return reply({'title': 'Dubioza kolektiv', 'value': 'рок', 'hook': 'впервые в Сербии', 'formula': 1,
                      'evidence': 'Prvi put u Srbiji nastupa bend'})
    await run_pass(pool, make_client(ai), ST, city='Belgrade', horizon_days=30, today=TODAY)
    b = await add_event(pool, 2, 'DUBIOZA KOLEKTIV', TEXT, day=TODAY + timedelta(days=1))
    res = await run_pass(pool, make_client(ai), ST, city='Belgrade', horizon_days=30, today=TODAY)
    assert len(calls) == 1 and res.copied == 1
    assert (await row(pool, b))['hook'] == 'впервые в Сербии' == (await row(pool, a))['hook']


@needs_pg
async def test_hooks_off_leaves_hook_empty_and_turning_on_regenerates(pool):
    eid = await add_event(pool, 1, 'DUBIOZA KOLEKTIV', TEXT)
    ai = lambda r: reply({'title': 'Dubioza kolektiv', 'value': 'рок', 'hook': 'впервые в Сербии', 'formula': 1,
                          'evidence': 'Prvi put u Srbiji nastupa bend'})
    await run_pass(pool, make_client(ai), OFF, city='Belgrade', horizon_days=30, today=TODAY)
    r = await row(pool, eid)
    assert r['hook'] is None and r['hook_status'] is None
    res = await run_pass(pool, make_client(ai), ST, city='Belgrade', horizon_days=30, today=TODAY)
    assert res.generated == 1 and (await row(pool, eid))['hook'] == 'впервые в Сербии'


@needs_pg
async def test_enrichment_cache_in_db_and_expiry(pool):
    e = Enricher(hook_settings({'method': 'on', 'enrich': {'method': 'on'}}).enrich, DbCache(pool),
                 transport=httpx.MockTransport(lambda r: httpx.Response(200, json={'artists': []}) if 'musicbrainz' in r.url.host else wp([{'missing': True}])))
    assert await e.facts_for('artist', 'Zoster') is None
    n = await pool.fetchval("SELECT count(*) FROM card_enrichment WHERE status='not_found'")
    assert n == 4                                                       # mb + wp ru/en/sr
    await e.facts_for('artist', 'Zoster')
    assert e.stats['lookups'] == 4
    await pool.execute("UPDATE card_enrichment SET expires_at = now() - interval '1 day'")
    await e.facts_for('artist', 'Zoster')
    assert e.stats['lookups'] == 8


# ------------------------------------------------------------------ строка в боте

def item(**kw):
    base = dict(kind='e', id=1, day=TODAY, title='ZOSTER', venue=None, time=None, ai_title='Zoster', ai_attr='рок',
                ai_profile='Концерты', ai_hook='сербская рок-группа из Белграда', ai_time=None)
    base.update(kw)
    return w.Item(**base)


def test_line_with_hook_and_without():
    on = card_line_settings({'method': 'on', 'hooks': {'method': 'on'}})
    wst = w.WindowSettings()
    t = w.line_text(item(time=time(21, 0)), wst, on)
    assert t == '🎸 Zoster — сербская рок-группа из Белграда · 21:00'
    off_hooks = card_line_settings({'method': 'on'})
    assert w.line_text(item(time=time(21, 0)), wst, off_hooks) == '🎸 Zoster · рок · 21:00'
    assert w.line_text(item(ai_hook=None), wst, on) == '🎸 Zoster · рок'


def test_line_time_from_text_only_when_db_time_empty_and_long_hook_capped():
    on = card_line_settings({'method': 'on', 'hooks': {'method': 'on', 'hook_max': 80}})
    wst = w.WindowSettings()
    assert w.line_text(item(ai_time=time(20, 0)), wst, on).endswith('· 20:00')
    assert w.line_text(item(time=time(21, 0), ai_time=time(20, 0)), wst, on).endswith('· 21:00')
    long = w.line_text(item(ai_hook='я' * 200), wst, on)
    assert len(long) <= w.LINE_CAP + 12


# ------------------------------------------------------------------ багфиксы по прогону на реальных концертах

def test_hook_rejects_foreign_script_and_cleans_dash_and_quotes():
    ev = {'formula': 1, 'evidence': 'povodom 20 godina karijere'}
    with pytest.raises(BadHook):
        chk({'hook': 'юбилей со向社会 20 лет', **ev})
    h = chk({'hook': '— юбилей: 20 лет на сцене', **ev})
    assert h.text == 'юбилей: 20 лет на сцене'
    h = chk({'hook': '20 лет на сцене, «Dubioza', **ev})
    assert '«' not in h.text


def test_title_keeps_paired_quotes_and_closes_missing_one():
    from card_lines.text import check_title
    src = 'Хор «Белградские мадригалисты» и ансамбль «Коло»'
    assert check_title('Хор «Белградские мадригалисты»', src, 40) == 'Хор «Белградские мадригалисты»'
    assert check_title('Хор «Белградские мадригалисты', src, 40) == 'Хор «Белградские мадригалисты»'
    assert check_title('«Коло»', src, 40) == 'Коло'
    assert check_title('"Коло"', src, 40) == 'Коло'
    with pytest.raises(Exception):
        check_title('Коло 向社会', src + ' 向社会', 40)


async def test_http_400_is_not_found_not_retry_and_generic_title_skipped():
    seen = []

    def h(req):
        seen.append(str(req.url))
        return httpx.Response(400) if 'musicbrainz' in req.url.host else wp([{'missing': True}])
    e = enricher(h)
    assert await e.facts_for('artist', 'Концерт националног ансамбла "КОЛО"') is None     # без EnrichUnavailable
    n = len(seen)
    assert await e.facts_for('artist', 'Концерты') is None and len(seen) == n               # общее название не ищем
    assert await e.facts_for('artist', 'очень длинное название события из семи слов') is None and len(seen) == n


# ------------------------------------------------------------------ багфиксы по второму прогону

def test_balance_quotes_respects_order():
    from card_lines.hooks import balance_quotes
    assert balance_quotes('Muštuluk», «Rano je» и «Kako') == 'Muštuluk, «Rano je» и Kako'
    assert balance_quotes('«Четыре времени года') == 'Четыре времени года'
    assert balance_quotes('песни «Frisson Noir»') == 'песни «Frisson Noir»'


def test_hook_length_slack_and_inflected_latin_word():
    ev = {'formula': 1, 'evidence': 'povodom 20 godina karijere'}
    ok = chk({'hook': 'повод: 20 лет карьеры Dubioza, ' + 'а' * 60, **ev})          # 80 < длина ≤ 100: принимается
    assert 80 < len(ok.text) <= 100
    with pytest.raises(BadHook):
        chk({'hook': 'повод: 20 лет карьеры Dubioza, ' + 'а' * 90, **ev})
    t = "Program: antologijske operske arije i melodije španske zarzuele"
    h = chk({'hook': 'арии и zarzuela', 'formula': 4, 'evidence': 'melodije španske zarzuele'}, text=t, title='X')
    assert h.text == 'арии и zarzuela'


async def test_musicbrainz_503_is_retried_then_succeeds_or_unavailable():
    calls = []

    def flaky(req):
        if 'musicbrainz' in req.url.host:
            calls.append(1)
            if len(calls) < 3:
                return httpx.Response(503)
            return mb([{'id': 'z', 'name': 'Zoster', 'score': 100, 'type': 'Group', 'tags': [{'name': 'rock', 'count': 1}]}])
        return wp([{'missing': True}])
    f = await enricher(flaky).facts_for('artist', 'Zoster')
    assert f and len(calls) == 3

    def down(req):
        return httpx.Response(503) if 'musicbrainz' in req.url.host else wp([{'missing': True}])
    with pytest.raises(EnrichUnavailable):
        await enricher(down).facts_for('artist', 'Zoster')
