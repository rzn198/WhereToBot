"""Окно категории с настоящей БД: тот же состав и порядок, что у ленты карточек; фильтры; скорость на большом дне (нужна DEDUP_TEST_DSN)."""
import os
import time as _time
from datetime import date, time, timedelta

import asyncpg
import pytest

from bot.handlers.category_window import load_items
from bot.handlers.events import EVENTS_CITY, load_event_ids
from bot.utils import window as w
from bot.utils.categories import horizon_days, window_rows_sql
from tests.test_fav_transfer_pg import event, post, put

DSN = os.getenv("DEDUP_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="нужна DEDUP_TEST_DSN (Postgres со схемой проекта)")
TODAY = date.today() + timedelta(days=2)       # заведомо в будущем при любом часовом поясе
U1 = 601


@pytest.fixture
async def pool():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=5)
    for t in ("dedup_decisions", "channels", "messages", "events", "favorites", "event_favorites", "hidden_events", "event_hidden"):
        await p.execute(f"TRUNCATE {t} RESTART IDENTITY CASCADE")
    yield p
    await p.close()


async def tag_event(pool, i, types=('Концерты',)):
    await pool.execute("UPDATE events SET event_types = $2 WHERE id = $1", i, list(types))


async def tag_post(pool, sid, types=('Концерты',)):
    await pool.execute("UPDATE messages SET event_types = $2 WHERE surrogate_id = $1", sid, list(types))


def refs(items):
    return [it.ref for it in items]


async def test_window_has_same_items_and_order_as_card_feed(pool):
    """Номер в окне = место в ленте карточек: посты раньше событий внутри дня, дни по порядку, скрытые не видны."""
    d1, d2 = TODAY, TODAY + timedelta(days=1)
    p1 = await post(pool, 1, day=d2, text="Пост второго дня")
    p2 = await post(pool, 2, day=d1, text="Пост первого дня")
    hidden_p = await post(pool, 3, day=d1, hidden=True)
    e1 = await event(pool, "u1", day=d2); e2 = await event(pool, "u2", day=d1); e3 = await event(pool, "u3", day=d1)
    hidden_e = await event(pool, "u4", day=d1, hidden=True); user_hidden = await event(pool, "u5", day=d1)
    other_cat = await event(pool, "u6", day=d1)
    for i in (e1, e2, e3, hidden_e, user_hidden):
        await tag_event(pool, i)
    await tag_event(pool, other_cat, ('Выставки/Ярмарки',))
    for sid in (p1, p2, hidden_p):
        await tag_post(pool, sid)
    await put(pool, U1, 'e', user_hidden, 'hid')

    items = await load_items(pool, U1, 'concerts', d1, d2)
    feed = await load_event_ids(pool, U1, f'cat:concerts:range:{d1}:{d2}')
    assert refs(items) == feed == [p2, f'e{e2}', f'e{e3}', p1, f'e{e1}']
    assert [it.day for it in items] == [d1, d1, d1, d2, d2]


async def test_single_day_equals_date_feed(pool):
    p = await post(pool, 1, day=TODAY); e = await event(pool, "u1", day=TODAY)
    await tag_post(pool, p); await tag_event(pool, e)
    await event(pool, "u2", day=TODAY + timedelta(days=1))
    items = await load_items(pool, U1, 'concerts', TODAY, TODAY)
    assert refs(items) == await load_event_ids(pool, U1, f'cat:concerts:date:{TODAY}') == [p, f'e{e}']


async def test_fields_for_line_event_time_venue_and_post_text(pool):
    e = await event(pool, "u1", day=TODAY)
    await pool.execute("UPDATE events SET title='Dubioza Kolektiv', venue='Štark Arena, Bulevar 58', starts_time='21:00', event_types=ARRAY['Концерты'] WHERE id=$1", e)
    p = await post(pool, 1, day=TODAY, text="🔥 Jazz night в клубе\nВход свободный")
    await tag_post(pool, p)
    items = await load_items(pool, U1, 'concerts', TODAY, TODAY)
    st = w.WindowSettings(show_venue=True)
    assert [w.build_line(n, it, st) for n, it in enumerate(items, 1)] == ['• Jazz night в клубе', '• Dubioza Kolektiv · 21:00 · Štark Arena']


async def test_post_text_is_cut_in_sql(pool):
    p = await post(pool, 1, day=TODAY, text="Начало " + "x" * 5000)
    await tag_post(pool, p)
    rows = await pool.fetch(window_rows_sql(), U1, EVENTS_CITY, horizon_days(), ['Концерты'], TODAY, TODAY)
    assert len(rows[0]['title']) == 400


async def test_other_city_and_past_days_excluded(pool):
    e = await event(pool, "u1", day=TODAY); await tag_event(pool, e)
    await pool.execute("UPDATE events SET city='Novi Sad' WHERE id=$1", e)
    old = await event(pool, "u2", day=date.today() - timedelta(days=3)); await tag_event(pool, old)
    assert await load_items(pool, U1, 'concerts', date.today() - timedelta(days=3), TODAY) == []


async def test_big_day_renders_fast_and_fits_one_message(pool):
    """28 записей (самый большой день из данных) и 1000 записей в категории: окно строится быстро и помещается в сообщение."""
    for i in range(28):
        e = await event(pool, f"big{i}", day=TODAY)
        await pool.execute("UPDATE events SET title=$2, venue='Клуб номер ' || $3, starts_time='20:30', event_types=ARRAY['Концерты'] WHERE id=$1",
                           e, f"Название большого концерта номер {i} " + "x" * (i % 30), str(i))
    for i in range(1000):
        e = await event(pool, f"bulk{i}", day=TODAY + timedelta(days=3 + i % 20)); await tag_event(pool, e)
    t0 = _time.perf_counter()
    items = await load_items(pool, U1, 'concerts', TODAY, TODAY)
    lay = w.layout_window(items, 'H', TODAY, TODAY, date.today(), w.WindowSettings())
    text, _ = w.render_text(lay, lambda it: 'https://t.me/bot?start=cw_concerts_20261120_20261120_e1234567')
    ms = (_time.perf_counter() - t0) * 1000
    assert len(items) == 28 and lay.pages == 2 and len(lay.shown) == 15 and len(text) < 4096
    assert ms < 500, f'окно на 28 записей строилось {ms:.0f} мс'
    # период на 3 недели по 1000 записям: список страницами, страница помещается в одно сообщение
    t0 = _time.perf_counter()
    items = await load_items(pool, U1, 'concerts', TODAY, TODAY + timedelta(days=22))
    lay = w.layout_window(items, 'H', TODAY, TODAY + timedelta(days=22), date.today(), w.WindowSettings())
    assert lay.mode == 'list' and lay.pages > 1 and len(w.render_text(lay)[0]) < 4096
    assert (_time.perf_counter() - t0) * 1000 < 1500
