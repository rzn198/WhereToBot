"""Окно киноафиши с БД: запросы, список/карточка/сеансы, устойчивость к устаревшим кнопкам (нужна DEDUP_TEST_DSN)."""
import os
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest

from bot.handlers import cinema_window as cw
from bot.handlers import cinema as old_cinema
from db import settings
from shared.config import config
from shared.tz import BELGRADE_TZ, belgrade_today

DSN = os.getenv("DEDUP_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="нужна DEDUP_TEST_DSN")
TOMORROW = belgrade_today() + timedelta(days=1)


def at(day, hh, mm=0):
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=BELGRADE_TZ)


@pytest.fixture
async def pool():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
    for t in ("cinema_events", "cinema_movie_aliases", "cinema_movies"):
        await p.execute(f"TRUNCATE {t} RESTART IDENTITY CASCADE")
    await p.execute("INSERT INTO cinema_descriptions(cinema_name, description) VALUES ('K1','Зал у реки') ON CONFLICT DO NOTHING")
    yield p
    await p.close()


async def add_movie(pool, key, en='', serbian='', ru='', genres=(), desc_src='', desc_ru=''):
    return await pool.fetchval(
        """INSERT INTO cinema_movies(title_key, title_serbian, title_en, title_ru, genres_ru, description_src, description_ru)
           VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING id""", key, serbian or key, en, ru, list(genres), desc_src, desc_ru)


async def add_ev(pool, movie_id, showtime, cinema='K1', source='cineplexx', poster=None, i=[0]):
    i[0] += 1
    await pool.execute(
        """INSERT INTO cinema_events(cinema_name, cinema_maps_url, cinema_url, movie_title, movie_title_serbian, showtime,
              ticket_url, source, movie_poster_url, movie_key) VALUES ($1,'https://maps/x','',$2,$2,$3,$4,$5,$6,$7)""",
        cinema, f'm{movie_id}', showtime, f'https://t/{i[0]}', source, poster, movie_id)


def make(data, pool, user_id=5):
    q = MagicMock(); q.data = data; q.answer = AsyncMock()
    upd = MagicMock(callback_query=q, message=None)
    upd.effective_user.id = user_id; upd.effective_chat.id = 1
    ctx = MagicMock(); ctx.user_data = {}; ctx.bot_data = {'pool': pool}
    return upd, ctx, q


@pytest.fixture
def shown(monkeypatch):
    m = AsyncMock()
    monkeypatch.setattr(cw, 'edit_or_send', m)
    monkeypatch.setattr(cw, 'bot_username', AsyncMock(return_value='TestBot'))
    return m


def buttons(call):
    return [b for r in call.kwargs['keyboard'].inline_keyboard for b in r]


async def test_day_films_sorted_with_session_counts_and_no_past_today(pool):
    b = await add_movie(pool, 'b', en='Beta', genres=['драма'])
    a = await add_movie(pool, 'a', en='alpha')
    for h in (10, 12, 20):
        await add_ev(pool, b, at(TOMORROW, h))
    await add_ev(pool, a, at(TOMORROW, 11))
    films = await cw.load_day_films(pool, TOMORROW)
    assert [(f['id'], f['sessions']) for f in films] == [(a, 1), (b, 3)]
    # прошлые сеансы сегодняшнего дня не считаются
    now = datetime.now(BELGRADE_TZ)
    c = await add_movie(pool, 'c', en='Gamma')
    await add_ev(pool, c, now - timedelta(hours=2))
    assert all(f['id'] != c for f in await cw.load_day_films(pool, now.date(), now))


async def test_poster_priority_and_filters(pool):
    m = await add_movie(pool, 'p', en='P')
    await add_ev(pool, m, at(TOMORROW, 10), source='cinestar', poster='https://cinestar/p.jpg')
    await add_ev(pool, m, at(TOMORROW, 11), source='cineplexx', poster='https://cineplexx/p.jpg')
    await add_ev(pool, m, at(TOMORROW, 12), source='cinegrand', poster='not-a-url')
    assert await cw.load_poster(pool, m) == 'https://cineplexx/p.jpg'
    m2 = await add_movie(pool, 'q', en='Q')
    await add_ev(pool, m2, at(TOMORROW, 10), poster='')
    assert await cw.load_poster(pool, m2) is None


async def test_list_screen_has_links_pager_and_day_row(pool, shown):
    size = cw.window_settings().page_size
    total = size + 2
    ids = [await add_movie(pool, f'k{i}', en=f'Film {i:02d}', genres=['драма']) for i in range(total)]
    for i in ids:
        await add_ev(pool, i, at(TOMORROW, 15))
    upd, ctx, q = make(f'cl:{TOMORROW.isoformat()}:2', pool)
    await cw.handle_cinema_list(upd, ctx)
    call = shown.call_args
    assert 'стр. 2/2' in call.kwargs['text'] and call.kwargs['text'].count('<a href=') == 2
    assert f'start=cm_{ids[size]}_{TOMORROW:%Y%m%d}' in call.kwargs['text']
    data = [b.callback_data for b in buttons(call)]
    assert f'cl:{TOMORROW.isoformat()}:1' in data and 'cld:today' in data and 'clc' in data


async def test_empty_day_message_without_jump(pool, shown):
    upd, ctx, q = make('cld:today', pool)
    await cw.handle_cinema_day(upd, ctx)
    assert 'На сегодня сеансов нет 🤷' in shown.call_args.kwargs['text']


async def test_bad_callbacks_and_far_dates(pool, shown):
    for data in ['cl:garbage', f'cl:{TOMORROW.isoformat()}:0', 'cl:2099-01-01', f'cl:{TOMORROW.isoformat()}:x']:
        upd, ctx, q = make(data, pool)
        await cw.handle_cinema_list(upd, ctx)
        q.answer.assert_awaited_with('Неверный запрос', show_alert=True)
    shown.assert_not_awaited()


async def test_card_carousel_wraps_and_back_goes_to_page(pool, shown):
    ids = [await add_movie(pool, f'c{i}', en=f'F{i}', desc_ru='слово ' * 100) for i in range(3)]
    for i in ids:
        await add_ev(pool, i, at(TOMORROW, 15))
    upd, ctx, q = make(f'cm:{ids[0]}:{TOMORROW.isoformat()}', pool)
    await cw.handle_cinema_card(upd, ctx)
    data = [b.callback_data for b in buttons(shown.call_args)]
    assert f'cm:{ids[2]}:{TOMORROW.isoformat()}:wp' in data and f'cm:{ids[1]}:{TOMORROW.isoformat()}' in data
    assert f'cmd:{ids[0]}:{TOMORROW.isoformat()}' in data and f'cl:{TOMORROW.isoformat()}:1' in data
    upd, ctx, q = make(f'cm:{ids[2]}:{TOMORROW.isoformat()}:wp', pool)
    await cw.handle_cinema_card(upd, ctx)
    q.answer.assert_awaited_with(cw.WRAP_TOASTS['wp'])


async def test_card_without_long_description_has_no_full_button(pool, shown):
    m = await add_movie(pool, 's', en='Short', desc_src='Коротко')
    await add_ev(pool, m, at(TOMORROW, 15))
    await cw.handle_cinema_card(*make(f'cm:{m}:{TOMORROW.isoformat()}', pool)[:2])
    assert not any(b.callback_data.startswith('cmd:') for b in buttons(shown.call_args))


async def test_card_for_vanished_film_shows_day_list(pool, shown):
    m = await add_movie(pool, 'z', en='Z')
    await cw.handle_cinema_card(*make(f'cm:{m}:{TOMORROW.isoformat()}', pool)[:2])
    assert 'сеансов нет' in shown.call_args.kwargs['text']


async def test_sessions_and_pick_validation(pool, shown):
    m = await add_movie(pool, 'x', en='X')
    await add_ev(pool, m, at(TOMORROW, 10))
    await add_ev(pool, m, at(TOMORROW, 12, 30), cinema='K1')
    d = TOMORROW.isoformat()
    await cw.handle_cinema_sessions(*make(f'cms:{m}:{d}', pool)[:2])
    cbs = [b.callback_data for b in buttons(shown.call_args)]
    assert f'cmp:{m}:{d}:0:1000' in cbs and f'cmp:{m}:{d}:1:1230' in cbs
    await cw.handle_cinema_pick(*make(f'cmp:{m}:{d}:1:1230', pool)[:2])
    kb = buttons(shown.call_args)
    assert any(b.url == 'https://maps/x' for b in kb) and any(b.text == '🎟 Билеты' for b in kb)
    assert 'Зал у реки' in shown.call_args.kwargs['text']
    # устаревшая кнопка (время не совпало) → заново список сеансов
    await cw.handle_cinema_pick(*make(f'cmp:{m}:{d}:1:2200', pool)[:2])
    assert 'Выберите сеанс' in shown.call_args.kwargs['text']


async def test_description_screen(pool, shown):
    m = await add_movie(pool, 'd', en='D', desc_ru='Полное описание фильма. ' * 30)
    await add_ev(pool, m, at(TOMORROW, 10))
    await cw.handle_cinema_desc(*make(f'cmd:{m}:{TOMORROW.isoformat()}', pool)[:2])
    assert shown.call_args.kwargs['text'].count('Полное описание фильма') > 10
    assert buttons(shown.call_args)[0].callback_data == f'cm:{m}:{TOMORROW.isoformat()}'


async def test_calendar_marks_only_days_with_sessions(pool, shown):
    m = await add_movie(pool, 'cal', en='Cal')
    await add_ev(pool, m, at(belgrade_today() + timedelta(days=2), 18))
    await cw.handle_cinema_calendar(*make('clc', pool)[:2])
    cbs = [b.callback_data for b in buttons(shown.call_args)]
    assert f'cl:{(belgrade_today() + timedelta(days=2)).isoformat()}' in cbs


async def test_failure_is_guarded_and_reported(pool, shown, monkeypatch):
    alert = AsyncMock(); monkeypatch.setattr(cw, '_alert_admin', alert)
    monkeypatch.setattr(cw, 'load_day_films', AsyncMock(side_effect=RuntimeError('boom')))
    await cw.handle_cinema_day(*make('cld:today', pool)[:2])
    alert.assert_awaited_once()
    assert 'Не получилось' in shown.call_args.kwargs['text']


async def test_old_menu_opens_new_window_by_mode(pool, monkeypatch):
    settings.clear_cache()
    monkeypatch.setitem(config['telegram'], 'admin_user_id', 1)
    opened = AsyncMock(); monkeypatch.setattr(cw, 'open_afisha', opened)
    await pool.execute("INSERT INTO bot_settings(key,value,updated_by) VALUES ('ui.cinema_mode','new_admin',1) "
                       "ON CONFLICT (key) DO UPDATE SET value='new_admin'")
    settings.clear_cache()
    assert await old_cinema._new_afisha_opened(*make('cinema:menu', pool, user_id=1)[:2]) is True
    settings.clear_cache()
    assert await old_cinema._new_afisha_opened(*make('cinema:menu', pool, user_id=2)[:2]) is False
    opened.assert_awaited_once()
