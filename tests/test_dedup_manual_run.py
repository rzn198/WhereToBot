"""Ручной запуск дедупа из админки: запросы в БД, подхват сервисом, экран бота (нужна DEDUP_TEST_DSN)."""
import os
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest

from bot.handlers import admin_dedup_run as h
from db import dedup_requests as dr
from deduplicator import main as svc
from deduplicator.schedule import ServiceSettings
from tests.test_dedup_service_pg import CFG, Outbox, client, finished_collector, two_posts

DSN = os.getenv("DEDUP_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="нужна DEDUP_TEST_DSN (Postgres со схемой проекта)")
ST = ServiceSettings(settle_seconds=60)


@pytest.fixture
async def pool():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    for t in ("dedup_requests", "dedup_decisions", "dedup_dates", "dedup_runs", "collector_runs", "channels", "messages", "events"):
        await p.execute(f"TRUNCATE {t} RESTART IDENTITY CASCADE")
    yield p
    await p.close()


# ------------------------------------------------------------------ db
async def test_only_one_open_request(pool):
    first = await dr.create(pool, 1, False)
    assert first and await dr.create(pool, 1, True) is None
    assert (await dr.open_request(pool))['status'] == 'pending'


async def test_cancel_only_pending_and_new_allowed_after(pool):
    await dr.create(pool, 1, False)
    assert await dr.cancel_pending(pool) is True and await dr.open_request(pool) is None
    assert await dr.create(pool, 1, True)
    req = await dr.claim(pool)
    assert req['status'] == 'running' and req['force'] is True
    assert await dr.cancel_pending(pool) is False                       # запущенный не отменяем
    assert await dr.create(pool, 1, False) is None


async def test_claim_release_finish_and_interrupted(pool):
    assert await dr.claim(pool) is None
    rid = await dr.create(pool, 1, False)
    req = await dr.claim(pool)
    await dr.release(pool, req['id'])
    assert (await dr.open_request(pool))['status'] == 'pending'
    await dr.claim(pool)
    assert await dr.close_interrupted(pool) == 1
    row = await pool.fetchrow("SELECT status, error FROM dedup_requests WHERE id = $1", rid)
    assert row['status'] == 'failed' and 'перезапуск' in row['error']


async def test_old_pending_expires(pool):
    await dr.create(pool, 1, False)
    await pool.execute("UPDATE dedup_requests SET requested_at = now() - interval '7 hours'")
    assert await dr.has_pending(pool) is False
    assert (await pool.fetchval("SELECT status FROM dedup_requests")) == 'cancelled'


# ------------------------------------------------------------------ сервис
async def test_tick_runs_manual_request_even_without_new_data_and_reports(pool):
    await two_posts(pool)
    out = Outbox()
    await svc.tick(pool, client(), CFG, ST, send=out)                    # обычный прогон
    assert await svc.tick(pool, client(), CFG, ST, send=out) is None     # новых данных нет
    n = len(out.texts)
    rid = await dr.create(pool, 1, True)
    res = await svc.tick(pool, client(), CFG, ST, send=out)
    assert res is not None and res.status == 'ok'
    row = await pool.fetchrow("SELECT status, run_id FROM dedup_requests WHERE id = $1", rid)
    assert row['status'] == 'done' and row['run_id'] == res.run_id
    assert any(t.startswith('✅ Ручной запуск дедупа (полный пересчёт)') for t in out.texts[n:])
    assert await svc.tick(pool, client(), CFG, ST, send=out) is None     # запрос закрыт — повторов нет


async def test_manual_request_waits_for_active_collector(pool):
    await two_posts(pool)
    out = Outbox()
    await svc.tick(pool, client(), CFG, ST, send=out)
    await dr.create(pool, 1, False)
    await pool.execute("INSERT INTO collector_runs (service) VALUES ('event_collector')")
    assert await svc.tick(pool, client(), CFG, ST, send=out) is None
    assert (await dr.open_request(pool))['status'] == 'pending'
    await pool.execute("UPDATE collector_runs SET finished_at = now(), status = 'ok'")   # settle не нужен для ручного
    assert (await svc.tick(pool, client(), CFG, ST, send=out)).status == 'ok'
    assert await dr.open_request(pool) is None


async def test_manual_request_stays_pending_when_lock_busy(pool):
    from deduplicator.lock import try_lock
    await two_posts(pool)
    await dr.create(pool, 1, False)
    async with try_lock(pool) as got:
        assert got
        assert await svc.tick(pool, client(), CFG, ST, send=Outbox()) is None
    assert (await dr.open_request(pool))['status'] == 'pending'


async def test_failed_manual_run_marks_request_failed_and_reports(pool):
    await two_posts(pool)
    out = Outbox()
    rid = await dr.create(pool, 1, False)
    res = await svc.tick(pool, client(available=False), CFG, ST, send=out)
    assert res.status == 'skipped'
    assert (await pool.fetchval("SELECT status FROM dedup_requests WHERE id = $1", rid)) == 'failed'
    assert any('Ручной запуск дедупа' in t and 'не выполнен' in t for t in out.texts)


async def test_manual_report_sent_even_if_alerts_off(pool):
    await two_posts(pool)
    out = Outbox()
    await dr.create(pool, 1, False)
    await svc.tick(pool, client(), CFG, ServiceSettings(alerts=False), send=out)
    assert len(out.texts) >= 1 and out.texts[0].startswith('✅ Ручной запуск')


async def test_legacy_method_ignores_requests(pool):
    await dr.create(pool, 1, False)
    assert await svc.tick(pool, client(), {**CFG, 'method': 'legacy'}, ST, send=Outbox()) is None
    assert (await dr.open_request(pool))['status'] == 'pending'


# ------------------------------------------------------------------ бот
def make(data):
    update = MagicMock()
    update.callback_query.answer = AsyncMock()
    update.callback_query.data = data
    update.effective_chat.id = 100
    update.effective_user.id = 999
    context = MagicMock()
    context.user_data = {}
    return update, context


async def run_handler(pool, data, method='ai', admin=True):
    update, context = make(data)
    context.bot_data = {'pool': pool}
    sent = AsyncMock()
    with patch.object(h, 'is_admin_user', return_value=admin), patch.object(h, 'edit_or_send', new=sent), \
         patch.object(h, '_method', return_value=method):
        await h.handle_dedup_run(update, context)
    return update, sent


def buttons(sent):
    kb = sent.call_args.kwargs['keyboard']
    return [b.callback_data for row in kb.inline_keyboard for b in row]


async def test_non_admin_is_refused(pool):
    update, sent = await run_handler(pool, 'ddr:run', admin=False)
    sent.assert_not_called()
    assert await dr.open_request(pool) is None
    assert update.callback_query.answer.call_args.kwargs.get('show_alert') is True


async def test_menu_shows_run_buttons_only_for_shadow_or_ai(pool):
    _, sent = await run_handler(pool, 'ddr:menu', method='ai')
    assert 'ddr:run' in buttons(sent) and 'ddr:force' in buttons(sent)
    _, sent = await run_handler(pool, 'ddr:menu', method='legacy')
    assert 'ddr:run' not in buttons(sent) and 'shadow или ai' in sent.call_args.kwargs['text']


async def test_run_creates_request_and_menu_shows_queue_with_cancel(pool):
    _, sent = await run_handler(pool, 'ddr:run')
    req = await dr.open_request(pool)
    assert req and req['force'] is False and req['requested_by'] == 999
    assert 'ddr:cancel' in buttons(sent) and 'ddr:run' not in buttons(sent)
    assert 'в очереди' in sent.call_args.kwargs['text']


async def test_force_asks_confirmation_then_creates_forced_request(pool):
    _, sent = await run_handler(pool, 'ddr:force')
    assert await dr.open_request(pool) is None and 'ddr:force_ok' in buttons(sent)
    await run_handler(pool, 'ddr:force_ok')
    assert (await dr.open_request(pool))['force'] is True


async def test_second_run_is_rejected_and_cancel_works(pool):
    await run_handler(pool, 'ddr:run')
    update, _ = await run_handler(pool, 'ddr:run')
    assert update.callback_query.answer.call_args.kwargs.get('show_alert') is True
    await run_handler(pool, 'ddr:cancel')
    assert await dr.open_request(pool) is None


async def test_run_refused_for_legacy_method(pool):
    update, _ = await run_handler(pool, 'ddr:run', method='legacy')
    assert await dr.open_request(pool) is None
    assert update.callback_query.answer.call_args.kwargs.get('show_alert') is True
