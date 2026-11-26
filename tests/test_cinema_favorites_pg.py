"""Избранное киноафиши: БД, экраны, пуш, очистка (нужна DEDUP_TEST_DSN)."""
import os
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest

from bot.handlers import cinema_window as cw
from bot.keyboards.event_nav import get_event_nav_keyboard
from bot.notifications import push
from db import cinema_favorites as cf
from shared.tz import BELGRADE_TZ, belgrade_today

DSN = os.getenv("DEDUP_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="нужна DEDUP_TEST_DSN")
TOMORROW = belgrade_today() + timedelta(days=1)
TODAY = belgrade_today()


def at(day, hh, mm=0):
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=BELGRADE_TZ)


@pytest.fixture
async def pool():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
    for t in ("cinema_favorites", "cinema_events", "cinema_movie_aliases", "cinema_movies"):
        await p.execute(f"TRUNCATE {t} RESTART IDENTITY CASCADE")
    yield p
    await p.close()


async def movie(pool, key='dune', en='Dune'):
    return await pool.fetchval(
        "INSERT INTO cinema_movies(title_key, title_serbian, title_en) VALUES ($1,$1,$2) RETURNING id", key, en)


async def ev(pool, mid, showtime, cinema='Cineplexx'):
    await pool.execute(
        """INSERT INTO cinema_events(cinema_name, cinema_maps_url, cinema_url, movie_title, movie_title_serbian, showtime,
              ticket_url, source, movie_key) VALUES ($1,'https://maps/x','','m','m',$2,'https://t/1','cineplexx',$3)""",
        cinema, showtime, mid)


def make(data, pool, user_id=5):
    q = MagicMock(); q.data = data; q.answer = AsyncMock()
    upd = MagicMock(callback_query=q, message=None)
    upd.effective_user.id = user_id; upd.effective_chat.id = 1
    ctx = MagicMock(); ctx.user_data = {}; ctx.bot_data = {'pool': pool}
    return upd, ctx, q


@pytest.fixture
def shown(monkeypatch):
    m = AsyncMock(); monkeypatch.setattr(cw, 'edit_or_send', m)
    return m


def btns(call):
    return [b for r in call.kwargs['keyboard'].inline_keyboard for b in r]


async def test_add_remove_exists_idempotent(pool):
    m = await movie(pool); t = at(TOMORROW, 19, 30)
    await cf.add(pool, 5, m, 'Cineplexx', t); await cf.add(pool, 5, m, 'Cineplexx', t)
    assert await cf.exists(pool, 5, m, 'Cineplexx', t) and await cf.count_upcoming(pool, 5) == 1
    assert not await cf.exists(pool, 6, m, 'Cineplexx', t)
    await cf.remove(pool, 5, m, 'Cineplexx', t)
    assert not await cf.exists(pool, 5, m, 'Cineplexx', t)


async def test_favorite_survives_events_rebuild_and_is_marked_removed(pool):
    m = await movie(pool); t = at(TOMORROW, 19)
    await ev(pool, m, t)
    await cf.add(pool, 5, m, 'Cineplexx', t)
    assert (await cf.list_upcoming(pool, 5))[0]['alive'] is True
    await pool.execute("DELETE FROM cinema_events")                      # сборщик пересобрал афишу, сеанса нет
    fav = (await cf.list_upcoming(pool, 5))[0]
    assert fav['alive'] is False
    await ev(pool, m, t)                                                  # сеанс вернулся — снова жив (id строк афиши другие)
    assert (await cf.list_upcoming(pool, 5))[0]['alive'] is True


async def test_past_not_listed_and_purged(pool):
    m = await movie(pool)
    await cf.add(pool, 5, m, 'K', datetime.now(BELGRADE_TZ) - timedelta(hours=2))
    await cf.add(pool, 5, m, 'K2', datetime.now(BELGRADE_TZ) - timedelta(days=2))
    assert await cf.list_upcoming(pool, 5) == []
    assert await cf.purge_past(pool) == 1                                 # старше суток
    assert await pool.fetchval("SELECT count(*) FROM cinema_favorites") == 1


async def test_movie_delete_cascades(pool):
    m = await movie(pool)
    await cf.add(pool, 5, m, 'K', at(TOMORROW, 10))
    await pool.execute("DELETE FROM cinema_movies WHERE id=$1", m)
    assert await pool.fetchval("SELECT count(*) FROM cinema_favorites") == 0


async def test_session_screen_toggle(pool, shown):
    m = await movie(pool); await ev(pool, m, at(TOMORROW, 19, 30))
    d = TOMORROW.isoformat()
    await cw.handle_cinema_pick(*make(f'cmp:{m}:{d}:0:1930', pool)[:2])
    assert any(b.callback_data == f'cfa:{m}:{d}:0:1930' for b in btns(shown.call_args))
    upd, ctx, q = make(f'cfa:{m}:{d}:0:1930', pool)
    await cw.handle_cinema_fav_toggle(upd, ctx)
    q.answer.assert_awaited_with('❤️ Добавлено в запланированное')
    assert await cf.count_upcoming(pool, 5) == 1
    assert any(b.callback_data == f'cfr:{m}:{d}:0:1930' for b in btns(shown.call_args))
    upd, ctx, q = make(f'cfr:{m}:{d}:0:1930', pool)
    await cw.handle_cinema_fav_toggle(upd, ctx)
    assert await cf.count_upcoming(pool, 5) == 0


async def test_stale_toggle_does_not_write(pool, shown):
    m = await movie(pool); await ev(pool, m, at(TOMORROW, 19, 30))
    await cw.handle_cinema_fav_toggle(*make(f'cfa:{m}:{TOMORROW.isoformat()}:0:2200', pool)[:2])
    assert await cf.count_upcoming(pool, 5) == 0
    assert 'Выберите сеанс' in shown.call_args.kwargs['text']


async def test_list_open_delete_and_ownership(pool, shown):
    m = await movie(pool); t = at(TOMORROW, 19)
    await ev(pool, m, t); await cf.add(pool, 5, m, 'Cineplexx', t)
    await cf.add(pool, 5, m, 'Gone', at(TOMORROW, 21))                    # такого сеанса в афише нет
    await cw.handle_cinema_fav_list(*make('cfl', pool)[:2])
    text = shown.call_args.kwargs['text']
    assert '🎬 Dune' in text and '❌ Dune' in text and 'сеанс снят' in text
    ids = [int(b.callback_data[4:]) for b in btns(shown.call_args) if b.callback_data.startswith('cfo:')]
    await cw.handle_cinema_fav_open(*make(f'cfo:{ids[0]}', pool)[:2])
    kb = btns(shown.call_args)
    assert any(b.text == '🎟 Билеты' for b in kb) and any(b.callback_data == f'cfd:{ids[0]}' for b in kb)
    await cw.handle_cinema_fav_open(*make(f'cfo:{ids[1]}', pool)[:2])
    assert 'Сеанс снят' in shown.call_args.kwargs['text'] and not any(b.text == '🎟 Билеты' for b in btns(shown.call_args))
    await cw.handle_cinema_fav_delete(*make(f'cfd:{ids[0]}', pool, user_id=6)[:2])   # чужой пользователь
    assert await cf.count_upcoming(pool, 5) == 2
    await cw.handle_cinema_fav_delete(*make(f'cfd:{ids[0]}', pool)[:2])
    assert await cf.count_upcoming(pool, 5) == 1


async def test_bad_ids(pool, shown):
    for h, d in ((cw.handle_cinema_fav_open, 'cfo:x'), (cw.handle_cinema_fav_delete, 'cfd:')):
        upd, ctx, q = make(d, pool)
        await h(upd, ctx)
        q.answer.assert_awaited_with('Неверный запрос', show_alert=True)


async def test_push_items_for_today_only_alive(pool):
    m = await movie(pool)
    now = datetime.now(BELGRADE_TZ)
    t1 = at(TODAY, 23, 0) if now.hour < 22 else now + timedelta(minutes=30)
    await ev(pool, m, t1); await cf.add(pool, 5, m, 'Cineplexx', t1)
    await cf.add(pool, 5, m, 'Gone', t1)                                  # снят — в пуш не идёт
    await cf.add(pool, 7, m, 'Cineplexx', at(TOMORROW, 10))              # другой день
    items = await push.fetch_items(pool, t1.astimezone(BELGRADE_TZ).date())
    assert [(i.kind, i.text, i.venue) for i in items[5]] == [('c', 'Dune', 'Cineplexx')]
    assert 7 not in items


def test_push_block_has_link_and_time():
    it = push.Item('c', 'Dune', 'Cineplexx', ref=17, when='19:30')
    parts = push.build_messages([it], date(2026, 11, 27) if False else TOMORROW, bot_username='MyBot')
    text, ents = parts[0]
    assert '1. 🎬 Dune · 19:30 — Cineplexx' in text
    assert ents and ents[0].url == f'https://t.me/MyBot?start=cm_17_{TOMORROW:%Y%m%d}'
    u16 = text.encode('utf-16-le')
    assert u16[ents[0].offset * 2:(ents[0].offset + ents[0].length) * 2].decode('utf-16-le') == 'Dune'
    no_link = push.build_messages([it], TOMORROW)[0][1]
    assert no_link == []


def test_nav_keyboard_shows_cinema_button_only_when_needed():
    def rows(n):
        kb = get_event_nav_keyboard(0, 1, 1, False, cinema_favs=n)
        return [b.callback_data for r in kb.inline_keyboard for b in r]
    assert 'cfl' in rows(2) and 'cfl' not in rows(0)


async def test_empty_favorites_with_cinema_shows_cinema_button(pool, monkeypatch):
    from bot.handlers import events
    m = await movie(pool); await cf.add(pool, 5, m, 'K', at(TOMORROW, 10))
    sent = AsyncMock(); monkeypatch.setattr(events, 'edit_or_send', sent)
    upd, ctx, q = make('x', pool)
    ctx.user_data.update(event_ids=[], current_filter='favorites')
    await events.show_current_event(upd, ctx, pool)
    assert [b.callback_data for r in sent.call_args.kwargs['keyboard'].inline_keyboard for b in r][0] == 'cfl'
    sent.reset_mock()
    ctx.user_data['current_filter'] = 'cat:x'
    await events.show_current_event(upd, ctx, pool)
    assert sent.call_args.kwargs['text'] == 'Событий не найдено 🤷'
