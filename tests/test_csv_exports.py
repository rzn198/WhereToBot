"""CSV-выгрузки: отчёт дедупликатора файлами и выгрузка пересмотров из админки (нужна DEDUP_TEST_DSN)."""
import csv
import io
import os
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import json

import asyncpg
import httpx
import pytest

from bot.handlers import review_export as rx
from db import review_export as dbx
from deduplicator import main as svc
from deduplicator import notify
from deduplicator.schedule import ServiceSettings
from shared.ai import AIClient

DSN = os.getenv("DEDUP_TEST_DSN")
needs_pg = pytest.mark.skipif(not DSN, reason="нужна DEDUP_TEST_DSN")
ST = ServiceSettings(settle_seconds=60)
DAY = date.today() + timedelta(days=5)
CFG = {"method": "ai", "ai": {"priority": [], "concurrency": 2}}


def client():
    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(
            {"groups": [{"ids": ["r1", "r2"], "reason": "то же"}]})}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}})
    return AIClient("dedup", "k", ["m1"], transport=httpx.MockTransport(handler), retry_delay=0)


async def two_posts(pool):
    for i, ch in ((1, 111), (2, 222)):
        await pool.execute(
            "INSERT INTO messages (id, position, channel_id, text, date, published_at) VALUES ($1,0,$2,$3,$4, now())",
            i, ch, "Концерт группы Икс в клубе Y, 20:00 — вход свободный " + str(ch), DAY)


async def finished_collector(pool, minutes_ago=10):
    await pool.execute(
        "INSERT INTO collector_runs (service, started_at, heartbeat_at, finished_at, status) VALUES "
        "('collector', now() - make_interval(mins => $1 + 1), now() - make_interval(mins => $1), now() - make_interval(mins => $1), 'ok')",
        minutes_ago)


class Outbox:
    def __init__(self):
        self.texts = []

    async def __call__(self, text):
        self.texts.append(text)


def parse(data: bytes):
    assert data.startswith(b'\xef\xbb\xbf')                      # BOM для Excel
    return list(csv.reader(io.StringIO(data.decode('utf-8-sig'))))


@pytest.fixture
async def pool():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    for t in ("event_reviews", "dedup_decisions", "dedup_dates", "dedup_runs", "collector_runs", "channels", "messages", "events"):
        await p.execute(f"TRUNCATE {t} RESTART IDENTITY CASCADE")
    yield p
    await p.close()


class Files:
    def __init__(self):
        self.items = []

    async def __call__(self, data, name, caption=''):
        self.items.append((name, data, caption))
        return True


# ------------------------------------------------------------------ дедупликатор

@needs_pg
async def test_tick_sends_text_and_two_csv_files(pool):
    await two_posts(pool)
    out, files = Outbox(), Files()
    res = await svc.tick(pool, client(), CFG, ST, send=out, send_file=files)
    assert res.stats["hidden"] == 1 and len(out.texts) == 1
    names = [n for n, _, _ in files.items]
    assert len(names) == 2 and names[0].startswith(f"dedup_run{res.run_id}_changes_") and names[1].startswith("dedup_current_")
    changes = parse(files.items[0][1])
    assert changes[0][:2] == ['Дата', 'Действие'] and len(changes) == 2
    assert changes[1][1] == 'скрыто' and 'Концерт группы Икс' in ' '.join(changes[1])
    current = parse(files.items[1][1])
    assert len(current) == 2 and current[1][1] == 'active'


@needs_pg
async def test_tick_without_send_file_and_custom_send_sends_no_files(pool):
    await two_posts(pool)
    with patch.object(notify, 'send_document', AsyncMock()) as doc:
        await svc.tick(pool, client(), CFG, ST, send=Outbox())
    doc.assert_not_awaited()


@needs_pg
async def test_no_changes_no_files(pool):
    out, files = Outbox(), Files()
    await finished_collector(pool)
    res = await svc.tick(pool, client(), CFG, ST, send=out, send_file=files)
    assert res is not None and files.items == []


@needs_pg
async def test_file_failure_does_not_break_service(pool):
    await two_posts(pool)

    async def boom(*a, **k):
        raise RuntimeError('tg down')
    res = await svc.tick(pool, client(), CFG, ST, send=Outbox(), send_file=boom)
    assert res.status == 'ok'


@needs_pg
async def test_current_snapshot_skips_past_days_and_reverted(pool):
    from deduplicator.report_csv import build_current
    assert await build_current(pool) is None
    await two_posts(pool)
    a, b = [r['surrogate_id'] for r in await pool.fetch("SELECT surrogate_id FROM messages ORDER BY id")]
    await pool.execute("INSERT INTO dedup_decisions (day, winner_kind, winner_id, loser_kind, loser_id, status) VALUES ($1,'m',$2,'m',$3,'active')", DAY, a, b)
    await pool.execute("INSERT INTO dedup_decisions (day, winner_kind, winner_id, loser_kind, loser_id, status) VALUES ($1,'m',$2,'m',$3,'stale')", date.today() - timedelta(days=3), a, b)
    await pool.execute("INSERT INTO dedup_decisions (day, winner_kind, winner_id, loser_kind, loser_id, status) VALUES ($1,'m',$2,'m',$3,'stale')", DAY, b, a)
    rows = parse(await build_current(pool))
    assert len(rows) == 2 and rows[1][0] == DAY.isoformat()


# ------------------------------------------------------------------ выгрузка пересмотров

async def _add(pool, flow, status, title):
    await pool.execute(
        "INSERT INTO event_reviews (flow, kind, event_id, reason_code, status, title, event_types, snapshot) "
        "VALUES ($1,'event',$2,$3,$4,$5,'{Концерты,Театр}','{}'::jsonb)",
        flow, abs(hash(title)) % 100000, {'review': 'manual', 'no_date': 'rejected', 'no_category': 'skipped'}[flow], status, title)


@needs_pg
async def test_scopes_select_expected_rows(pool):
    await _add(pool, 'review', 'open', 'a')
    await _add(pool, 'review', 'applied', 'b')
    await _add(pool, 'no_date', 'resolved', 'c')
    await _add(pool, 'no_category', 'resolved', 'd')
    counts = {s: len(await dbx.fetch_rows(pool, s)) for s in dbx.SCOPES}
    assert counts == {'open': 1, 'all': 4, 'nd': 1, 'nc': 1}
    rows = parse(dbx.to_csv_bytes(await dbx.fetch_rows(pool, 'all')))
    assert rows[0] == dbx.COLUMNS and rows[1][rows[0].index('event_types')] == 'Концерты|Театр'


def _update(data):
    upd = MagicMock()
    upd.effective_user.id = 1
    upd.effective_chat.id = 5
    upd.callback_query.data = data
    upd.callback_query.answer = AsyncMock()
    ctx = MagicMock()
    ctx.bot_data = {'pool': MagicMock()}
    ctx.bot.send_document = AsyncMock()
    return upd, ctx


async def test_handler_rejects_non_admin():
    upd, ctx = _update('rvx:get:all')
    with patch.object(rx, 'is_admin_user', return_value=False), patch.object(dbx, 'fetch_rows', AsyncMock()) as fr:
        await rx.handle_review_export(upd, ctx)
    fr.assert_not_awaited() and ctx.bot.send_document.assert_not_awaited()


async def test_handler_sends_file_with_caption():
    upd, ctx = _update('rvx:get:open')
    row = {c: None for c in dbx.COLUMNS}
    with patch.object(rx, 'is_admin_user', return_value=True), patch.object(dbx, 'fetch_rows', AsyncMock(return_value=[row])):
        await rx.handle_review_export(upd, ctx)
    kw = ctx.bot.send_document.await_args.kwargs
    assert kw['filename'].startswith('event_reviews_open_') and kw['filename'].endswith('.csv') and '1 шт.' in kw['caption']


async def test_handler_empty_and_unknown_scope():
    upd, ctx = _update('rvx:get:nd')
    with patch.object(rx, 'is_admin_user', return_value=True), patch.object(dbx, 'fetch_rows', AsyncMock(return_value=[])):
        await rx.handle_review_export(upd, ctx)
    ctx.bot.send_document.assert_not_awaited()
    upd2, ctx2 = _update('rvx:get:zzz')
    with patch.object(rx, 'is_admin_user', return_value=True):
        await rx.handle_review_export(upd2, ctx2)
    ctx2.bot.send_document.assert_not_awaited()


def test_keyboard_and_admin_button():
    kb = rx.build_menu_keyboard()
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert cbs == ['rvx:get:open', 'rvx:get:all', 'rvx:get:nd', 'rvx:get:nc', 'admin_panel:main']
    assert all(len(c.encode()) <= 64 for c in cbs)
