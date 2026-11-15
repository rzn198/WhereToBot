"""Сверка с листингом, этап 1: решающая функция, fetch polako с фейковым API, PG-сравнение без изменений в events."""
import json
import os
from datetime import date, timedelta
from types import SimpleNamespace

import httpx
import pytest

from event_collector import stale
from event_collector.scrapers import polako


def rows(n, prefix='u'):
    return [{'id': i, 'source_url': f'{prefix}{i}'} for i in range(n)]


def test_decide_ok_and_stale_list():
    db = rows(200)
    listing = {f'u{i}' for i in range(200) if i % 10}      # 10% нет в листинге
    d = stale.decide(db, listing, True)
    assert d.ok and len(d.stale_ids) == 20


@pytest.mark.parametrize("listing,complete,reason", [
    (None, True, 'listing_incomplete'),
    ({f'u{i}' for i in range(200)}, False, 'listing_incomplete'),
    ({f'u{i}' for i in range(50)}, True, 'listing_too_small'),
])
def test_decide_skips(listing, complete, reason):
    d = stale.decide(rows(200), listing, complete)
    assert not d.ok and d.reason.startswith(reason) and d.stale_ids == []


def test_decide_skips_when_share_too_high():
    db = rows(200)
    d = stale.decide(db, {f'u{i}' for i in range(100, 200)} | {f'x{i}' for i in range(50)}, True)
    assert not d.ok and 'share_too_high' in d.reason


def test_decide_skips_when_listing_below_half_of_db():
    d = stale.decide(rows(1000), {f'u{i}' for i in range(120)}, True)
    assert not d.ok and 'below_half' in d.reason


# ---- polako.fetch с фейковым API ----
def _ev(i):
    return {"id": i, "title": f"Event {i}", "start_date": "2099-01-01", "location": {"name": "v"}}


def _patch_api(monkeypatch, total, pages_fail_at=None, recount=None, id_dup=False):
    real = httpx.AsyncClient

    def handler(request):
        q = request.url.params
        if q.get("aggregate[count]"):
            handler.calls += 1
            n = total if handler.calls == 1 or recount is None else recount
            return httpx.Response(200, json={"data": [{"count": {"id": str(n)}}]})
        off = int(q["offset"])
        if pages_fail_at is not None and off >= pages_fail_at:
            return httpx.Response(500)
        lim = int(q["limit"])
        ids = range(off, min(off + lim, total))
        return httpx.Response(200, json={"data": [_ev(0 if id_dup and i == 1 else i + 1) for i in ids]})
    handler.calls = 0
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(polako, "PAGE_DELAY", 0)
    monkeypatch.setattr(polako, "PAGE_SIZE", 50)


async def _fetch(scr):
    return await scr.fetch()


@pytest.mark.asyncio
async def test_polako_listing_complete(monkeypatch):
    _patch_api(monkeypatch, 120)
    scr = polako.PolakoScraper()
    await _fetch(scr)
    assert scr.listing_complete and len(scr.listing_urls) == 120


@pytest.mark.asyncio
async def test_polako_listing_incomplete_on_page_error(monkeypatch):
    _patch_api(monkeypatch, 120, pages_fail_at=100)
    scr = polako.PolakoScraper()
    await _fetch(scr)
    assert not scr.listing_complete


@pytest.mark.asyncio
async def test_polako_listing_incomplete_when_total_changes(monkeypatch):
    _patch_api(monkeypatch, 120, recount=121)
    scr = polako.PolakoScraper()
    await _fetch(scr)
    assert not scr.listing_complete


@pytest.mark.asyncio
async def test_polako_listing_incomplete_when_urls_dup(monkeypatch):
    _patch_api(monkeypatch, 120, id_dup=True)
    scr = polako.PolakoScraper()
    await _fetch(scr)
    assert not scr.listing_complete


# ---- PG ----
import asyncpg

DSN = os.getenv("DEDUP_TEST_DSN")
pg = pytest.mark.skipif(not DSN, reason="нужна DEDUP_TEST_DSN")


@pytest.fixture
async def pool():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
    await p.execute("TRUNCATE events RESTART IDENTITY CASCADE")
    yield p
    await p.close()


@pg
@pytest.mark.asyncio
async def test_run_shadow_logs_and_changes_nothing(pool, tmp_path):
    fut = date.today() + timedelta(days=5)
    for i in range(150):
        await pool.execute(
            "INSERT INTO events (source, source_url, title, venue, starts_at, city, event_types) "
            "VALUES ('polako',$1,$2,'v',$3,'Belgrade',ARRAY['Концерты'])", f'p/{i}', f't{i}', fut)
    await pool.execute(
        "INSERT INTO events (source, source_url, title, venue, starts_at, city, event_types) "
        "VALUES ('other','o/1','x','v',$1,'Belgrade',ARRAY['Концерты'])", fut)
    before = await pool.fetch("SELECT * FROM events ORDER BY id")
    scr = SimpleNamespace(listing_urls={f'p/{i}' for i in range(140)}, listing_complete=True)
    path = tmp_path / 'stale_audit.jsonl'
    d = await stale.run_shadow(pool, 'polako', scr,
                               cfg={'mode': 'shadow', 'max_share': 0.3, 'min_listing': 100}, audit_path=path)
    assert d.ok and len(d.stale_ids) == 10
    rec = json.loads(path.read_text(encoding='utf-8').splitlines()[0])
    assert rec['stale'] == 10 and rec['db_future'] == 150 and len(rec['items']) == 10
    assert [dict(r) for r in await pool.fetch("SELECT * FROM events ORDER BY id")] == [dict(r) for r in before]


@pg
@pytest.mark.asyncio
async def test_run_shadow_off_and_skip_write_nothing(pool, tmp_path):
    path = tmp_path / 'a.jsonl'
    scr = SimpleNamespace(listing_urls=None, listing_complete=False)
    assert await stale.run_shadow(pool, 'polako', scr, cfg={'mode': 'off', 'max_share': .3, 'min_listing': 100}, audit_path=path) is None
    d = await stale.run_shadow(pool, 'polako', scr, cfg={'mode': 'shadow', 'max_share': .3, 'min_listing': 100}, audit_path=path)
    assert not d.ok and not path.exists()


async def _seed(pool, n=150, hidden_ids=()):
    fut = date.today() + timedelta(days=5)
    for i in range(n):
        await pool.execute(
            "INSERT INTO events (source, source_url, title, venue, starts_at, city, event_types, hidden_by_admin) "
            "VALUES ('polako',$1,$2,'v',$3,'Belgrade',ARRAY['Концерты'],$4)", f'p/{i}', f't{i}', fut, i in hidden_ids)
    await pool.execute(
        "INSERT INTO events (source, source_url, title, venue, starts_at, city, event_types) "
        "VALUES ('other','o/1','x','v',$1,'Belgrade',ARRAY['Концерты'])", fut)
    await pool.execute(
        "INSERT INTO events (source, source_url, title, venue, starts_at, city, event_types) "
        "VALUES ('polako','p/past','old','v',$1,'Belgrade',ARRAY['Концерты'])", date.today() - timedelta(days=3))


HIDE = {'mode': 'hide', 'max_share': 0.3, 'min_listing': 100}


@pg
@pytest.mark.asyncio
async def test_hide_mode_hides_only_missing_future_polako(pool, tmp_path):
    await _seed(pool, hidden_ids={0})                                  # p/0 уже скрыта и отсутствует в листинге
    scr = SimpleNamespace(listing_urls={f'p/{i}' for i in range(10, 150)}, listing_complete=True)
    path = tmp_path / 'a.jsonl'
    d = await stale.run_shadow(pool, 'polako', scr, cfg=HIDE, audit_path=path)
    assert d.ok and len(d.stale_ids) == 10
    h = {r['source_url']: r['hidden_by_admin'] for r in await pool.fetch("SELECT source_url, hidden_by_admin FROM events")}
    assert all(h[f'p/{i}'] for i in range(10))
    assert not any(h[f'p/{i}'] for i in range(10, 150))
    assert h['o/1'] is False and h['p/past'] is False                  # чужой источник и прошедшая дата не тронуты
    rec = json.loads(path.read_text(encoding='utf-8').splitlines()[0])
    assert rec['hidden'] == 9 and rec['mode'] == 'hide'                # p/0 уже была скрыта
    # повтор: ничего нового
    await stale.run_shadow(pool, 'polako', scr, cfg=HIDE, audit_path=path)
    assert json.loads(path.read_text(encoding='utf-8').splitlines()[1])['hidden'] == 0


@pg
@pytest.mark.asyncio
@pytest.mark.parametrize("listing_n,complete", [(140, False), (50, True), (60, True)])
async def test_hide_mode_protections_hide_nothing(pool, tmp_path, listing_n, complete):
    await _seed(pool)
    scr = SimpleNamespace(listing_urls={f'p/{i}' for i in range(listing_n)}, listing_complete=complete)
    d = await stale.run_shadow(pool, 'polako', scr, cfg=HIDE, audit_path=tmp_path / 'a.jsonl')
    assert not d.ok
    assert await pool.fetchval("SELECT count(*) FROM events WHERE hidden_by_admin") == 0


@pg
@pytest.mark.asyncio
async def test_hide_mode_share_over_limit_hides_nothing(pool, tmp_path):
    await _seed(pool)
    scr = SimpleNamespace(listing_urls={f'p/{i}' for i in range(60, 150)} | {f'n/{i}' for i in range(40)}, listing_complete=True)
    d = await stale.run_shadow(pool, 'polako', scr, cfg=HIDE, audit_path=tmp_path / 'a.jsonl')
    assert not d.ok and 'share_too_high' in d.reason
    assert await pool.fetchval("SELECT count(*) FROM events WHERE hidden_by_admin") == 0


def test_unknown_mode_does_nothing():
    import asyncio
    assert asyncio.run(stale.run_shadow(None, 'polako', SimpleNamespace(), cfg={'mode': 'bogus', 'max_share': .3, 'min_listing': 1})) is None
