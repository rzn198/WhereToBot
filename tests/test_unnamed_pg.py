"""Очереди «Неразобранные», решения и защита ручной категории на настоящей БД (нужна DEDUP_TEST_DSN)."""
import os
from datetime import date, datetime, timedelta

import asyncpg
import pytest

from bot.handlers import reviews as rv
from bot.handlers.events import EVENTS_CITY
from bot.utils import moderation as mod
from event_collector.scrapers.base import EventItem
from event_collector.saver import save_events

DSN = os.getenv("DEDUP_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="нужна DEDUP_TEST_DSN")
DAY = date.today() + timedelta(days=3)


@pytest.fixture
async def pool():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
    for t in ("event_reviews", "messages", "channels", "events"):
        await p.execute(f"TRUNCATE {t} RESTART IDENTITY CASCADE")
    yield p
    await p.close()


async def add_event(pool, url, day=DAY, types=(), hidden=False, city=EVENTS_CITY):
    return await pool.fetchval(
        "INSERT INTO events (source, source_url, title, description, starts_at, venue, city, event_types, hidden_by_admin) "
        "VALUES ('ticketsrs', $1, 't '||$1, 'd', $2, 'v', $3, $4, $5) RETURNING id",
        url, day, city, list(types), hidden)


async def add_post(pool, mid, day=None, types=(), hidden=False):
    await pool.execute("INSERT INTO channels (channel_id, username, title) VALUES (10, 'chan', 'K') ON CONFLICT DO NOTHING")
    return await pool.fetchval(
        "INSERT INTO messages (id, channel_id, text, date, published_at, event_types, hidden_by_admin) "
        "VALUES ($1::bigint, 10, 'Пост '||$1::text, $2, now(), $3, $4) RETURNING surrogate_id", mid, day, list(types), hidden)


async def test_no_date_queue_only_undecided_visible_posts(pool):
    a = await add_post(pool, 1)
    b = await add_post(pool, 2)
    await add_post(pool, 3, hidden=True)
    await add_post(pool, 4, day=DAY)
    assert await mod.load_queue(pool, 'no_date', EVENTS_CITY) == [a, b]
    assert await mod.decide(pool, a, flow='no_date', reason_code='rejected') == 'done'
    assert await mod.load_queue(pool, 'no_date', EVENTS_CITY) == [b]
    assert await pool.fetchval("SELECT date FROM messages WHERE surrogate_id=$1", a) is None   # дату не вносили


async def test_set_date_updates_post_and_logs_decision(pool):
    a = await add_post(pool, 1)
    assert await mod.decide(pool, a, flow='no_date', reason_code='set_date', correct_date=DAY) == 'done'
    assert await pool.fetchval("SELECT date FROM messages WHERE surrogate_id=$1", a) == DAY
    r = await pool.fetchrow("SELECT * FROM event_reviews")
    assert (r['flow'], r['reason_code'], r['status'], r['correct_date']) == ('no_date', 'set_date', 'resolved', DAY)
    assert r['resolved_at'] is not None


async def test_set_date_does_not_overwrite_existing_date(pool):
    a = await add_post(pool, 1, day=DAY)
    assert await mod.decide(pool, a, flow='no_date', reason_code='set_date', correct_date=DAY + timedelta(days=9)) == 'changed'
    assert await pool.fetchval("SELECT date FROM messages WHERE surrogate_id=$1", a) == DAY
    assert await pool.fetchval("SELECT count(*) FROM event_reviews") == 0


async def test_no_category_queue_filters_and_order(pool):
    e_late = await add_event(pool, 'late', day=DAY + timedelta(days=5))
    e_soon = await add_event(pool, 'soon', day=DAY)
    p_mid = await add_post(pool, 1, day=DAY + timedelta(days=2))
    db_today = await pool.fetchval('SELECT CURRENT_DATE')      # часовой пояс БД может отличаться от python
    await add_event(pool, 'past', day=db_today - timedelta(days=1))
    await add_event(pool, 'other_city', city='Zagreb')
    await add_event(pool, 'has_cat', types=('Концерты',))
    await add_event(pool, 'hid', hidden=True)
    await add_post(pool, 2, day=DAY, types=('Концерты',))
    q = await mod.load_queue(pool, 'no_category', EVENTS_CITY)
    assert q == [f'e{e_soon}', p_mid, f'e{e_late}']


async def test_set_category_replaces_array_and_protects_event(pool):
    e = await add_event(pool, 'x', types=())
    ref = f'e{e}'
    assert await mod.decide(pool, ref, flow='no_category', reason_code='set_category', correct_category='Театр') == 'done'
    row = await pool.fetchrow("SELECT event_types, category_manual FROM events WHERE id=$1", e)
    assert row['event_types'] == ['Театр'] and row['category_manual'] is True
    r = await pool.fetchrow("SELECT * FROM event_reviews")
    assert (r['flow'], r['reason_code'], r['correct_category'], r['status']) == ('no_category', 'set_category', 'Театр', 'resolved')
    assert await mod.load_queue(pool, 'no_category', EVENTS_CITY) == []


async def test_set_category_on_post_does_not_touch_manual_flag(pool):
    p = await add_post(pool, 1, day=DAY)
    await mod.decide(pool, p, flow='no_category', reason_code='set_category', correct_category='Театр')
    assert await pool.fetchval("SELECT event_types FROM messages WHERE surrogate_id=$1", p) == ['Театр']


async def test_skip_and_hide(pool):
    a = await add_event(pool, 'a')
    b = await add_event(pool, 'b')
    await mod.decide(pool, f'e{a}', flow='no_category', reason_code='skipped')
    assert await pool.fetchval("SELECT hidden_by_admin FROM events WHERE id=$1", a) is False
    await mod.decide(pool, f'e{b}', flow='no_category', reason_code='hidden')
    assert await pool.fetchval("SELECT hidden_by_admin FROM events WHERE id=$1", b) is True
    assert await mod.load_queue(pool, 'no_category', EVENTS_CITY) == []


async def test_queues_are_independent(pool):
    p = await add_post(pool, 1, day=DAY)          # есть дата, нет категории
    q = await add_post(pool, 2)                   # нет ни даты, ни категории
    await mod.decide(pool, q, flow='no_date', reason_code='rejected')
    assert await mod.load_queue(pool, 'no_date', EVENTS_CITY) == []
    assert await mod.load_queue(pool, 'no_category', EVENTS_CITY) == [p]    # у q даты нет → в эту очередь не входит


async def test_decision_is_unique_per_flow(pool):
    a = await add_event(pool, 'a')
    await mod.decide(pool, f'e{a}', flow='no_category', reason_code='skipped')
    await mod.decide(pool, f'e{a}', flow='no_category', reason_code='skipped')
    assert await pool.fetchval("SELECT count(*) FROM event_reviews") == 1


async def test_missing_record(pool):
    assert await mod.decide(pool, 'e999', flow='no_category', reason_code='skipped') == 'missing'


async def test_review_real_category_is_applied_and_button_state(pool):
    e = await add_event(pool, 'x', types=('Концерты',))
    ref = f'e{e}'
    assert await rv.save_review(pool, ref, 'wrong_category', correct_category='Театр') == 'created'
    row = await pool.fetchrow("SELECT event_types, category_manual FROM events WHERE id=$1", e)
    assert row['event_types'] == ['Театр'] and row['category_manual'] is True
    r = await pool.fetchrow("SELECT * FROM event_reviews")
    assert (r['flow'], r['status']) == ('review', 'applied')
    assert await rv.review_state(pool, ref) == 'applied'


async def test_review_other_only_logs(pool):
    e = await add_event(pool, 'x', types=('Концерты',))
    await rv.save_review(pool, f'e{e}', 'wrong_category', correct_category='Другое')
    row = await pool.fetchrow("SELECT event_types, category_manual FROM events WHERE id=$1", e)
    assert row['event_types'] == ['Концерты'] and row['category_manual'] is False
    assert await rv.review_state(pool, f'e{e}') == 'open'


async def test_review_date_and_manual_do_not_touch_tables(pool):
    e = await add_event(pool, 'x', types=('Концерты',))
    await rv.save_review(pool, f'e{e}', 'wrong_date', correct_date=DAY + timedelta(days=30))
    assert await pool.fetchval("SELECT starts_at FROM events WHERE id=$1", e) == DAY


async def test_save_events_keeps_manual_category(pool):
    item = EventItem(source='ticketsrs', source_url='u1', title='t', starts_at=datetime.combine(DAY, datetime.min.time()),
                     event_types=['Концерты'], city=EVENTS_CITY)
    await save_events(pool, [item])
    eid = await pool.fetchval("SELECT id FROM events WHERE source_url='u1'")
    await mod.decide(pool, f'e{eid}', flow='no_category', reason_code='set_category', correct_category='Театр')
    item.event_types = ['Музеи']
    await save_events(pool, [item])
    assert await pool.fetchval("SELECT event_types FROM events WHERE id=$1", eid) == ['Театр']


async def test_review_cleanup_removes_applied_and_resolved(pool):
    from db.review_cleanup import ReviewCleanupSettings, purge_reviews
    a = await add_event(pool, 'a')
    await mod.decide(pool, f'e{a}', flow='no_category', reason_code='skipped')
    await rv.save_review(pool, f'e{a}', 'wrong_category', correct_category='Театр')
    await pool.execute("UPDATE event_reviews SET created_at = now() - interval '10 days'")
    assert await purge_reviews(pool, ReviewCleanupSettings(date.today(), True)) == 2


async def test_migration_013_idempotent(pool):
    sql = open(os.path.join(os.path.dirname(__file__), '..', 'migrations', '013_unnamed_queues.sql'), encoding='utf-8').read()
    await pool.execute(sql)
    await pool.execute(sql)
