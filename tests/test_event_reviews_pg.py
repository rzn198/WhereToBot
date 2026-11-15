"""Пересмотры и ленты по дням на настоящей БД (нужна DEDUP_TEST_DSN: Postgres со схемой проекта)."""
import os
from datetime import date, timedelta

import asyncpg
import pytest

from bot.handlers import reviews as rv
from bot.handlers.events import EVENTS_CITY, load_event_ids
from bot.utils.categories import category_dates_sql, horizon_days

DSN = os.getenv("DEDUP_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="нужна DEDUP_TEST_DSN (Postgres со схемой проекта)")
DAY = date.today() + timedelta(days=3)
DAY2 = date.today() + timedelta(days=5)


@pytest.fixture
async def pool():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
    for t in ("event_reviews", "event_hidden", "event_favorites", "hidden_events", "favorites", "messages", "channels", "events"):
        await p.execute(f"TRUNCATE {t} RESTART IDENTITY CASCADE")
    yield p
    await p.close()


async def add_event(pool, url, day=DAY, types=("Концерты",), hidden=False):
    return await pool.fetchval(
        "INSERT INTO events (source, source_url, title, description, starts_at, venue, city, event_types, hidden_by_admin) "
        "VALUES ('ticketsrs', $1, 'Название '||$1, 'Полный текст события', $2, 'Площадка', $3, $4, $5) RETURNING id",
        url, day, EVENTS_CITY, list(types), hidden)


async def add_post(pool, day=DAY, types=("Концерты",)):
    await pool.execute("INSERT INTO channels (channel_id, username, title) VALUES (10, 'chan', 'Канал')")
    return await pool.fetchval(
        "INSERT INTO messages (id, channel_id, text, date, published_at, event_types) "
        "VALUES (77, 10, 'Заголовок поста\nтекст', $1, now(), $2) RETURNING surrogate_id", day, list(types))


async def test_save_event_review_stores_full_snapshot(pool):
    eid = await add_event(pool, "http://a")
    ref = f"e{eid}"
    assert await rv.is_under_review(pool, ref) is False
    assert await rv.save_review(pool, ref, 'wrong_category', correct_category='Другое') == 'created'
    row = await pool.fetchrow("SELECT * FROM event_reviews")
    assert row['kind'] == 'event' and row['event_id'] == eid and row['message_id'] is None
    assert row['reason_code'] == 'wrong_category' and row['correct_category'] == 'Другое'
    assert row['title'] == 'Название http://a' and row['description'] == 'Полный текст события'
    assert row['starts_at'] == DAY and row['source'] == 'ticketsrs' and row['source_url'] == 'http://a'
    assert row['event_types'] == ['Концерты'] and row['status'] == 'open'
    snap = row['snapshot']
    import json
    snap = json.loads(snap) if isinstance(snap, str) else snap
    assert snap['title'] == 'Название http://a' and snap['venue'] == 'Площадка' and snap['starts_at'] == DAY.isoformat()
    assert await rv.is_under_review(pool, ref) is True


async def test_event_in_events_table_is_not_changed_by_review(pool):
    eid = await add_event(pool, "http://a")
    await rv.save_review(pool, f"e{eid}", 'wrong_category', correct_category='Другое')
    ev = await pool.fetchrow("SELECT event_types, starts_at, hidden_by_admin FROM events WHERE id=$1", eid)
    assert ev['event_types'] == ['Концерты'] and ev['starts_at'] == DAY and ev['hidden_by_admin'] is False


async def test_second_review_while_open_is_not_duplicated(pool):
    eid = await add_event(pool, "http://a")
    assert await rv.save_review(pool, f"e{eid}", 'manual', reason_text='x') == 'created'
    assert await rv.save_review(pool, f"e{eid}", 'wrong_date', correct_date=DAY2) == 'exists'
    assert await pool.fetchval("SELECT count(*) FROM event_reviews") == 1


async def test_resolved_review_makes_button_active_again_and_allows_new(pool):
    eid = await add_event(pool, "http://a")
    await rv.save_review(pool, f"e{eid}", 'manual', reason_text='x')
    await pool.execute("UPDATE event_reviews SET status='resolved', resolved_at=now()")
    assert await rv.is_under_review(pool, f"e{eid}") is False
    assert await rv.save_review(pool, f"e{eid}", 'manual', reason_text='y') == 'created'
    assert await pool.fetchval("SELECT count(*) FROM event_reviews") == 2


async def test_wrong_date_and_manual_fields(pool):
    a, b = await add_event(pool, "http://a"), await add_event(pool, "http://b")
    await rv.save_review(pool, f"e{a}", 'wrong_date', correct_date=DAY2)
    await rv.save_review(pool, f"e{b}", 'manual', reason_text='Это курс, а не концерт')
    rows = {r['event_id']: r for r in await pool.fetch("SELECT * FROM event_reviews")}
    assert rows[a]['correct_date'] == DAY2 and rows[a]['reason_text'] is None
    assert rows[b]['reason_text'] == 'Это курс, а не концерт' and rows[b]['correct_date'] is None


async def test_post_review_and_independent_from_event_with_same_number(pool):
    eid = await add_event(pool, "http://a")
    mid = await add_post(pool)
    assert await rv.save_review(pool, mid, 'manual', reason_text='плохой пост') == 'created'
    row = await pool.fetchrow("SELECT * FROM event_reviews")
    assert row['kind'] == 'post' and row['message_id'] == mid and row['event_id'] is None
    assert row['source'] == '@chan' and row['source_url'] == 'https://t.me/chan/77' and row['title'] == 'Заголовок поста'
    assert await rv.is_under_review(pool, mid) is True
    assert await rv.is_under_review(pool, f"e{eid}") is False       # одинаковые числа в разных таблицах не пересекаются


async def test_missing_record(pool):
    assert await rv.save_review(pool, 'e999', 'manual', reason_text='x') == 'missing'
    assert await rv.save_review(pool, 999, 'manual', reason_text='x') == 'missing'


async def test_review_survives_event_deletion(pool):
    eid = await add_event(pool, "http://a")
    await rv.save_review(pool, f"e{eid}", 'manual', reason_text='x')
    await pool.execute("DELETE FROM events WHERE id=$1", eid)
    assert await pool.fetchval("SELECT title FROM event_reviews") == 'Название http://a'


async def test_category_day_feed_and_calendar_dates(pool):
    a = await add_event(pool, "http://a", DAY, ("Концерты",))
    await add_event(pool, "http://b", DAY2, ("Концерты",))
    await add_event(pool, "http://c", DAY, ("Кино/Театр",))
    await add_event(pool, "http://d", DAY, ("Концерты",), hidden=True)
    multi = await add_event(pool, "http://m", DAY, ("Кино/Театр", "Концерты"))
    refs = await load_event_ids(pool, 1, f"cat:concerts:date:{DAY.isoformat()}")
    assert set(refs) == {f"e{a}", f"e{multi}"}                        # мульти-категория входит, скрытое и чужое — нет
    theater = await load_event_ids(pool, 1, f"cat:theater:date:{DAY.isoformat()}")
    assert multi_in(theater, multi)
    row = await pool.fetchrow(category_dates_sql(), 1, EVENTS_CITY, horizon_days(), ["Концерты"])
    assert set(row['dates']) == {DAY, DAY2} and row['max_date'] == DAY2


def multi_in(refs, eid):
    return f"e{eid}" in refs


async def test_category_day_feed_includes_posts_and_respects_user_hide(pool):
    mid = await add_post(pool)
    eid = await add_event(pool, "http://a")
    refs = await load_event_ids(pool, 1, f"cat:concerts:date:{DAY.isoformat()}")
    assert refs == [mid, f"e{eid}"]                                   # посты раньше событий внутри дня
    await pool.execute("INSERT INTO event_hidden (user_id, event_id) VALUES (1, $1)", eid)
    assert await load_event_ids(pool, 1, f"cat:concerts:date:{DAY.isoformat()}") == [mid]
    assert len(await load_event_ids(pool, 2, f"cat:concerts:date:{DAY.isoformat()}")) == 2


async def test_migration_is_idempotent(pool):
    sql = open(os.path.join(os.path.dirname(__file__), '..', 'migrations', '012_event_reviews.sql'), encoding='utf-8').read()
    await pool.execute(sql)
    await pool.execute(sql)


async def test_purge_reviews_by_date_and_status(pool):
    from datetime import date as d
    from db.review_cleanup import ReviewCleanupSettings, purge_reviews
    ids = [await add_event(pool, f"http://{i}") for i in range(4)]
    for e in ids:
        await rv.save_review(pool, f"e{e}", 'manual', reason_text='x')
    rows = [r['id'] for r in await pool.fetch("SELECT id FROM event_reviews ORDER BY id")]
    await pool.execute("UPDATE event_reviews SET created_at = now() - interval '40 days' WHERE id = ANY($1)", rows[:3])
    await pool.execute("UPDATE event_reviews SET status='resolved' WHERE id = ANY($1)", [rows[0], rows[3]])
    cutoff = date.today() - timedelta(days=10)
    # только закрытые и старше даты: удалится одна (rows[0]); rows[3] закрыт, но свежий; rows[1], rows[2] старые, но открыты
    assert await purge_reviews(pool, ReviewCleanupSettings(cutoff, True)) == 1
    assert {r['id'] for r in await pool.fetch("SELECT id FROM event_reviews")} == set(rows[1:])
    # вместе с открытыми: уйдут оба старых открытых, свежий остаётся
    assert await purge_reviews(pool, ReviewCleanupSettings(cutoff, False)) == 2
    assert [r['id'] for r in await pool.fetch("SELECT id FROM event_reviews")] == [rows[3]]


async def test_admin_menu_counts_agree_with_feed(pool):
    from bot.utils.categories import load_menu_counts
    await add_event(pool, "http://a", DAY, ("Концерты",))
    await add_event(pool, "http://b", DAY, ("Кино/Театр", "Концерты"))
    await add_event(pool, "http://c", DAY, ("Кино/Театр",))
    await add_event(pool, "http://d", DAY, ("Кино/Театр", "Выставки/Ярмарки"))
    await add_event(pool, "http://e", DAY, ("Игры и развлечения",))
    await add_event(pool, "http://h", DAY, ("Кино/Театр",), hidden=True)
    c = await load_menu_counts(pool, 1, EVENTS_CITY)
    assert c['concerts'] == 2 and c['fun'] == 1 and c['theater'] == 3 and c['expo'] == 1
    assert c['rest'] == 3                      # b, c, d: мульти-категория b/d считается один раз; «Концерты» и «Игры» не входят
    feed = await load_event_ids(pool, 1, "cat:concerts")
    assert len(feed) == c['concerts']          # число на кнопке = длина ленты
