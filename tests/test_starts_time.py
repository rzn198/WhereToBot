"""Время начала события (events.starts_time): скрейпер ticketsrs, сохранение, ссылка на карточке; категория «Музеи»."""
import os
from datetime import date, datetime, time, timedelta, timezone

import pytest

from bot.formatters.event_card import build_event_card
from bot.utils.categories import MAIN_MENU_CODES, get_category, rest_categories
from event_collector.scrapers.base import EventItem
from event_collector.scrapers.ticketsrs import TicketsRsScraper
from shared.categories import MUSEUMS

NOW = datetime(2026, 11, 11, tzinfo=timezone.utc)


def build(date_time, title="Crvenkapa"):
    ev = {"Title": title, "Slug": "event/crvenkapa_1", "StartDate": "2026-12-01", "DateTime": date_time, "Venue": "v"}
    return TicketsRsScraper()._build_item(ev, "Семейные/детские", NOW)


# ── скрейпер ──────────────────────────────────────────────────────────────────────────────────────────────

def test_listing_time_is_kept():
    it = build("1. decembar 2026 18:30")
    assert it.starts_time == time(18, 30) and it.starts_at.hour == 18


@pytest.mark.parametrize("dt", ["1. decembar 2026", "utorak 1.decembar 18h", "", None])
def test_listing_without_clock_gives_no_time(dt):
    assert build(dt).starts_time is None


def test_listing_midnight_is_not_a_time():
    assert build("1. decembar 2026 00:00").starts_time is None


@pytest.mark.parametrize("data, expected", [
    ({"EventTime": "2026-11-22T12:00:00"}, time(12, 0)),
    ({"EventTime": "2026-11-19T19:30:10"}, time(19, 30)),
    ({"EventTime": "2026-11-22T00:00:00"}, None),     # заглушка сайта
    ({"EventTime": "2026-11-22"}, None),
    ({"EventTime": "2026-11-22T25:99:00"}, None),
    ({}, None), (None, None), ("x", None),
])
def test_time_from_event_detail(data, expected):
    assert TicketsRsScraper._time_from_event(data) == expected


def test_venue_from_keeps_old_contract():
    assert TicketsRsScraper._venue_from({"Venue": {"Name": "Puž", "Town": "Beograd"}}) == ("Puž", "Beograd")
    assert TicketsRsScraper._venue_from({"Venue": "x"}) == (None, None)
    assert TicketsRsScraper._venue_from(None) == (None, None)


def test_other_sources_default_to_no_time():
    it = EventItem(source="x", source_url="u", title="t", starts_at=datetime(2026, 12, 1, 20, 0))
    assert it.starts_time is None


# ── карточка ─────────────────────────────────────────────────────────────────────────────────────────────

def test_card_link_shows_time():
    row = {"title": "Crvenkapa", "starts_at": date(2026, 12, 1), "description": "",
           "source_url": "https://tickets.rs/event/crvenkapa_1", "starts_time": time(11, 5)}
    assert "🎟 Билеты и подробности · tickets.rs · 11:05" in build_event_card(row, expanded=True).text


def test_card_link_without_time_is_unchanged():
    row = {"title": "Crvenkapa", "starts_at": date(2026, 12, 1), "description": "",
           "source_url": "https://tickets.rs/event/crvenkapa_1"}
    text = build_event_card(row, expanded=True).text
    assert text.endswith("🎟 Билеты и подробности · tickets.rs")
    row["starts_time"] = None
    assert build_event_card(row, expanded=True).text == text


def test_card_link_time_without_host():
    row = {"title": "t", "starts_at": date(2026, 12, 1), "description": "", "source_url": "tg", "starts_time": time(9, 0)}
    assert "🎟 Билеты и подробности · 09:00" in build_event_card(row, expanded=True).text


# ── категория «Музеи» ────────────────────────────────────────────────────────────────────────────────────

def test_museums_is_a_category_in_rest_menu():
    c = get_category("museums")
    assert c and c.label == "Музеи" and c.db_values == (MUSEUMS,)
    assert "museums" not in MAIN_MENU_CODES
    assert c in rest_categories()
    assert len(f"cat:{c.code}:today".encode()) <= 64


# ── Postgres ─────────────────────────────────────────────────────────────────────────────────────────────
import asyncpg

DSN = os.getenv("DEDUP_TEST_DSN")
pg = pytest.mark.skipif(not DSN, reason="нужна DEDUP_TEST_DSN")


@pytest.fixture
async def pool():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
    await p.execute("TRUNCATE events RESTART IDENTITY CASCADE")
    yield p
    await p.close()


def item(url, t, day):
    return EventItem(source="ticketsrs", source_url=url, title="Uspavana lepotica",
                     starts_at=datetime(day.year, day.month, day.day, 0, 0), starts_time=t, venue="Puž", city="Belgrade")


@pg
async def test_saver_stores_and_updates_time(pool):
    from event_collector.saver import save_events
    day = (datetime.now() + timedelta(days=10)).date()
    await save_events(pool, [item("u/1", time(11, 0), day), item("u/2", time(17, 0), day), item("u/3", None, day)])
    rows = {r["source_url"]: r["starts_time"] for r in await pool.fetch("SELECT source_url, starts_time FROM events")}
    assert rows == {"u/1": time(11, 0), "u/2": time(17, 0), "u/3": None}

    # повторное сохранение того же — без изменений; смена времени — обновляется (в т.ч. «появилось время»)
    await save_events(pool, [item("u/1", time(11, 0), day), item("u/3", time(12, 30), day)])
    rows = {r["source_url"]: r["starts_time"] for r in await pool.fetch("SELECT source_url, starts_time FROM events")}
    assert rows == {"u/1": time(11, 0), "u/2": time(17, 0), "u/3": time(12, 30)}
    await save_events(pool, [item("u/1", time(11, 30), day)])
    assert await pool.fetchval("SELECT starts_time FROM events WHERE source_url='u/1'") == time(11, 30)


@pg
async def test_only_time_change_counts_as_update(pool):
    """Раньше UPSERT сравнивал только прежние колонки: событие, у которого поменялось лишь время, считалось бы неизменным."""
    from event_collector.saver import save_events
    day = (datetime.now() + timedelta(days=10)).date()
    await save_events(pool, [item("u/1", None, day)])
    await save_events(pool, [item("u/1", time(19, 0), day)])
    assert await pool.fetchval("SELECT starts_time FROM events") == time(19, 0)


@pg
async def test_event_card_query_returns_time(pool):
    from bot.handlers.events import fetch_event_by_id
    from bot.utils.refs import event_ref
    from event_collector.saver import save_events
    day = (datetime.now() + timedelta(days=10)).date()
    await save_events(pool, [item("https://tickets.rs/event/x_1", time(11, 0), day)])
    eid = await pool.fetchval("SELECT id FROM events")
    res = await fetch_event_by_id(pool, event_ref(eid), 1)
    assert res["starts_time"] == time(11, 0)
    assert "tickets.rs · 11:00" in build_event_card(res, expanded=True).text
