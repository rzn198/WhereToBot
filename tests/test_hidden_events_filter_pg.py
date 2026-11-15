"""Скрытые AI события сайтов не попадают в ленты, счётчики и календарь бота (нужна DEDUP_TEST_DSN)."""
import os
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest

from bot.handlers import calendar as cal
from bot.handlers.events import EVENTS_CITY, fetch_event_by_id, load_event_ids
from bot.utils.categories import CATEGORIES, counts_sql, feed_sql, horizon_days
from bot.utils.refs import event_ref

DSN = os.getenv("DEDUP_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="нужна DEDUP_TEST_DSN (Postgres со схемой проекта)")
DAY = date.today() + timedelta(days=3)
USER = 1


@pytest.fixture
async def pool():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
    for t in ("event_hidden", "event_favorites", "hidden_events", "favorites", "messages", "events"):
        await p.execute(f"TRUNCATE {t} RESTART IDENTITY CASCADE")
    yield p
    await p.close()


async def add_event(pool, url, hidden, types=("Концерты",)):
    return await pool.fetchval(
        "INSERT INTO events (source, source_url, title, starts_at, city, event_types, hidden_by_admin) "
        "VALUES ('biletrs', $1, $1, $2, $3, $4, $5) RETURNING id",
        url, DAY, EVENTS_CITY, list(types), hidden)


def event_ids(refs):
    return {r for r in refs if not isinstance(r, int)}


async def test_date_feed_hides_hidden_events(pool):
    vis = await add_event(pool, "http://a", False)
    await add_event(pool, "http://b", True)
    refs = await load_event_ids(pool, USER, f"date:{DAY.isoformat()}")
    assert len(refs) == 1 and str(vis) in str(refs[0])


async def test_category_feed_and_counter_agree_and_hide(pool):
    await add_event(pool, "http://a", False)
    await add_event(pool, "http://b", True)
    await add_event(pool, "http://c", True)
    cat = CATEGORIES[0]
    feed = await pool.fetch(feed_sql(), USER, EVENTS_CITY, horizon_days(), list(cat.db_values))
    counts = await pool.fetchrow(counts_sql(), USER, EVENTS_CITY, horizon_days(), *[list(c.db_values) for c in CATEGORIES])
    assert len(feed) == 1
    assert counts["c0"] == len(feed)           # число на кнопке = длина ленты


async def test_calendar_ignores_days_with_only_hidden_events(pool):
    await add_event(pool, "http://b", True)
    captured = {}

    def fake_build(**kw):
        captured.update(kw)
        return []

    update, context = MagicMock(), MagicMock()
    update.effective_chat.id = 1
    context.user_data = {}
    with patch.object(cal, "build_calendar", fake_build), patch.object(cal, "_edit_contact_window", AsyncMock()):
        await cal._show_calendar(update, context, pool)
    assert DAY not in captured["dates_with_events"]

    await add_event(pool, "http://a", False)
    with patch.object(cal, "build_calendar", fake_build), patch.object(cal, "_edit_contact_window", AsyncMock()):
        await cal._show_calendar(update, context, pool)
    assert DAY in captured["dates_with_events"]


async def test_hidden_event_still_opens_by_id(pool):
    """Старая кнопка в чате не должна приводить к «записи нет»."""
    hid = await add_event(pool, "http://b", True)
    row = await fetch_event_by_id(pool, event_ref(hid), USER)
    assert row is not None and row["id"] == hid


async def test_favorites_unchanged(pool):
    """Скрытое админом/дедупом событие в списке «Избранное» не показывается (его избранное уезжает на победителя)."""
    hid = await add_event(pool, "http://b", True)
    await pool.execute("INSERT INTO event_favorites (user_id, event_id) VALUES ($1, $2)", USER, hid)
    refs = await load_event_ids(pool, USER, "favorites")
    assert refs == []
