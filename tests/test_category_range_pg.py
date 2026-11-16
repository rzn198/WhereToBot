"""Счётчик периода совпадает с лентой периода категории (нужна DEDUP_TEST_DSN)."""
import os
from datetime import date, timedelta

import asyncpg
import pytest

from bot.handlers.events import EVENTS_CITY, load_event_ids
from bot.utils.categories import horizon_days, period_count_sql
from tests.test_fav_transfer_pg import event, post, put

DSN = os.getenv("DEDUP_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="нужна DEDUP_TEST_DSN (Postgres со схемой проекта)")
TODAY = date.today()
D = [TODAY + timedelta(days=i) for i in range(0, 6)]
U1 = 501


@pytest.fixture
async def pool():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=5)
    for t in ("dedup_decisions", "channels", "messages", "events", "favorites", "event_favorites", "hidden_events", "event_hidden"):
        await p.execute(f"TRUNCATE {t} RESTART IDENTITY CASCADE")
    yield p
    await p.close()


async def tag_event(pool, i, types):
    await pool.execute("UPDATE events SET event_types = $2 WHERE id = $1", i, types)


async def tag_post(pool, sid, types):
    await pool.execute("UPDATE messages SET event_types = $2 WHERE surrogate_id = $1", sid, types)


async def count(pool, first, last, values=('Концерты',)):
    return await pool.fetchval(period_count_sql(), U1, EVENTS_CITY, horizon_days(), list(values), first, last)


async def test_period_count_equals_feed_length_and_applies_all_filters(pool):
    e1 = await event(pool, "https://t/e1", day=D[1]); await tag_event(pool, e1, ["Концерты"])
    e2 = await event(pool, "https://t/e2", day=D[2]); await tag_event(pool, e2, ["Концерты", "Фестивали"])
    e_out = await event(pool, "https://t/e3", day=D[5]); await tag_event(pool, e_out, ["Концерты"])          # вне периода
    e_other = await event(pool, "https://t/e4", day=D[1]); await tag_event(pool, e_other, ["Спорт/активный отдых"])
    e_hid = await event(pool, "https://t/e5", day=D[1], hidden=True); await tag_event(pool, e_hid, ["Концерты"])
    e_user_hid = await event(pool, "https://t/e6", day=D[2]); await tag_event(pool, e_user_hid, ["Концерты"])
    p1 = await post(pool, 1, ch=11, day=D[1]); await tag_post(pool, p1, ["Концерты"])
    p_hid = await post(pool, 2, ch=11, day=D[2], hidden=True); await tag_post(pool, p_hid, ["Концерты"])
    await put(pool, U1, "e", e_user_hid, "hid")

    ids = await load_event_ids(pool, U1, f"cat:concerts:range:{D[1].isoformat()}:{D[3].isoformat()}")
    assert sorted(map(str, ids)) == sorted(map(str, [p1, f"e{e1}", f"e{e2}"]))
    assert await count(pool, D[1], D[3]) == len(ids) == 3
    assert await count(pool, D[1], D[1]) == 2                 # один день: пост + событие
    assert await count(pool, D[0], D[0]) == 0
    assert await count(pool, D[1], D[5]) == 4                 # с дальним событием


async def test_other_city_is_not_counted(pool):
    e = await event(pool, "https://t/e9", day=D[1]); await tag_event(pool, e, ["Концерты"])
    await pool.execute("UPDATE events SET city = 'Novi Sad' WHERE id = $1", e)
    assert await count(pool, D[1], D[1]) == 0
    assert await load_event_ids(pool, U1, f"cat:concerts:range:{D[1].isoformat()}:{D[1].isoformat()}") == []
