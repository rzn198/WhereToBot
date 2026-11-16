"""Замыкание «по кругу»: месяцы календарей, карточки лент, фильмы афиши, админские списки (без БД)."""
import asyncio
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.handlers import admin_dedup_ai, admin_panel, categories as cats, cinema, navigation
from bot.handlers.calendar import build_calendar, month_carousel, parse_nav_target

TODAY = date.today()


def month_first(offset):
    d = TODAY.replace(day=1)
    for _ in range(offset):
        d = (d + timedelta(days=32)).replace(day=1)
    return d


M0, M1, M3 = month_first(0), month_first(1), month_first(3)
DATES = {TODAY, M1.replace(day=15), M3.replace(day=15)}
MAXD = M3.replace(day=15)


def ym(d):
    return (d.year, d.month)


def make_update(data, user_data=None):
    q = MagicMock(); q.data = data; q.answer = AsyncMock()
    upd = MagicMock(callback_query=q)
    upd.effective_user.id = 1; upd.effective_chat.id = 1
    ctx = MagicMock(); ctx.user_data = dict(user_data or {}); ctx.bot_data = {'pool': MagicMock()}
    return upd, ctx, q


# ---------- месяцы ----------

def test_carousel_goes_through_months_with_events_and_wraps():
    c = month_carousel(*ym(M0), DATES, MAXD)
    assert c['next'] == (ym(M1), False) and c['prev'] == (ym(M3), True)          # назад из первого — самый дальний
    c = month_carousel(*ym(M1), DATES, MAXD)
    assert c['prev'] == (ym(M0), False) and c['next'] == (ym(M3), False)         # пустые месяцы пропускаем
    c = month_carousel(*ym(M3), DATES, MAXD)
    assert c['next'] == (ym(M0), True) and c['prev'] == (ym(M1), False)          # вперёд из последнего — самый ранний


def test_carousel_single_month_has_no_targets():
    c = month_carousel(*ym(M0), {TODAY}, TODAY)
    assert c == {'prev': None, 'next': None}


def test_past_and_beyond_max_dates_do_not_count():
    c = month_carousel(*ym(M0), {TODAY - timedelta(days=40), TODAY, M3.replace(day=20)}, M1)
    assert c == {'prev': None, 'next': None}


def test_calendar_header_has_arrows_with_targets_and_wrap_flags():
    rows = build_calendar(M0.year, M0.month, DATES, MAXD, 'cinema:date', 'cinema_cal_nav')
    prev, title, nxt = rows[0]
    assert prev.callback_data == f'cinema_cal_nav:{M3.year}-{M3.month:02d}:wp'
    assert nxt.callback_data == f'cinema_cal_nav:{M1.year}-{M1.month:02d}'
    assert title.text.endswith(str(M0.year)) and title.callback_data == 'catnoop'
    assert [b.text for b in rows[1]] == ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс']
    assert not any(b.callback_data.endswith(':prev') and 'nav' in b.callback_data for r in rows for b in r)   # нижнего ряда стрелок нет


def test_all_nav_callbacks_fit_limit():
    for code in ('festival', 'museums'):
        rows = build_calendar(M3.year, M3.month, DATES, MAXD, f'catpick:{code}', f'catcal_nav:{code}')
        assert all(len(b.callback_data.encode()) <= 64 for r in rows for b in r)


def test_parse_nav_target_new_and_legacy_formats():
    assert parse_nav_target(['2027-03', 'wp'], 2026, 11) == (2027, 3, '🔁 К самому дальнему месяцу')
    assert parse_nav_target(['2027-03', 'wn'], 2026, 11)[2] == '🔁 К ближайшему месяцу'
    assert parse_nav_target(['2027-03'], 2026, 11) == (2027, 3, None)
    assert parse_nav_target(['prev'], 2026, 1) == (2025, 12, None) and parse_nav_target(['next'], 2026, 12) == (2027, 1, None)
    assert parse_nav_target(['2027-13'], 2026, 11) is None and parse_nav_target(['abc'], 2026, 11) is None
    assert parse_nav_target([], 2026, 11) is None


async def test_category_nav_jumps_to_target_month_with_toast():
    upd, ctx, q = make_update('catcal_nav:fun:2027-03:wp')
    with patch.object(cats, '_show_category_calendar', AsyncMock()) as show:
        await cats.handle_category_calendar_nav(upd, ctx)
    q.answer.assert_awaited_once_with('🔁 К самому дальнему месяцу')
    assert (ctx.user_data['calendar_year'], ctx.user_data['calendar_month']) == (2027, 3)
    show.assert_awaited_once_with(upd, ctx, 'fun')


async def test_category_nav_rejects_garbage():
    for data in ('catcal_nav:fun:zzz', 'catcal_nav:nope:2027-03', 'catcal_nav:fun'):
        upd, ctx, q = make_update(data)
        await cats.handle_category_calendar_nav(upd, ctx)
        assert q.answer.await_args.kwargs.get('show_alert') is True


async def test_cinema_and_admin_calendar_nav_use_target_month():
    upd, ctx, q = make_update('cinema_cal_nav:2027-01:wn')
    with patch.object(cinema, '_show_cinema_calendar', AsyncMock()) as show:
        await cinema.handle_cinema_calendar_nav(upd, ctx)
    assert (ctx.user_data['cinema_calendar_year'], ctx.user_data['cinema_calendar_month']) == (2027, 1)
    q.answer.assert_awaited_once_with('🔁 К ближайшему месяцу')
    show.assert_awaited_once()
    upd, ctx, q = make_update('admin_hidden_nav:2027-02')
    with patch.object(admin_panel, '_show_admin_hidden_calendar', AsyncMock()) as show, \
         patch.object(admin_panel, '_require_admin', AsyncMock(return_value=True)):
        await admin_panel.handle_admin_hidden_nav(upd, ctx)
    assert (ctx.user_data['admin_hidden_year'], ctx.user_data['admin_hidden_month']) == (2027, 2)


# ---------- карточки лент ----------

async def press(data, ids, index):
    upd, ctx, q = make_update(data, {'event_ids': list(ids), 'current_index': index})
    with patch.object(navigation, 'show_current_event', AsyncMock()) as show, patch.object(navigation, 'DEBOUNCE_DELAY', 0):
        await navigation.handle_navigation(upd, ctx)
        task = ctx.user_data.get('nav_debounce_task')
        if task:
            await task
    return ctx.user_data['current_index'], q.answer.await_args, show


async def test_cards_wrap_both_ways_with_toast():
    idx, call, show = await press('nav:prev', ['e1', 'e2', 'e3'], 0)
    assert idx == 2 and call.args == ('🔁 К концу списка',)
    show.assert_awaited_once()
    idx, call, _ = await press('nav:next', ['e1', 'e2', 'e3'], 2)
    assert idx == 0 and call.args == ('🔁 К началу списка',)


async def test_cards_inside_list_move_without_toast():
    idx, call, _ = await press('nav:next', ['e1', 'e2', 'e3'], 0)
    assert idx == 1 and call.args == (None,)
    idx, call, _ = await press('nav:prev', ['e1', 'e2', 'e3'], 2)
    assert idx == 1 and call.args == (None,)


async def test_single_card_does_not_move_and_says_so():
    idx, call, show = await press('nav:next', ['e1'], 0)
    assert idx == 0 and call.args == ('Это единственное событие',)
    show.assert_not_awaited()


# ---------- афиша кинотеатров ----------

async def cinema_press(direction, count, index):
    upd, ctx, q = make_update(f'cinema:{direction}', {'cinema_movies': [{'id': i} for i in range(count)], 'cinema_index': index})
    with patch.object(cinema, '_show_cinema_movie', AsyncMock()) as show:
        await cinema.handle_cinema_nav(upd, ctx)
    return ctx.user_data['cinema_index'], q.answer.await_args, show


async def test_cinema_movies_wrap_by_title_list():
    idx, call, show = await cinema_press('prev', 4, 0)
    assert idx == 3 and call.args == ('🔁 К последнему фильму',)
    show.assert_awaited_once()
    idx, call, _ = await cinema_press('next', 4, 3)
    assert idx == 0 and call.args == ('🔁 К первому фильму',)
    idx, call, _ = await cinema_press('next', 4, 1)
    assert idx == 2 and call.args == (None,)


async def test_cinema_single_movie_stays():
    idx, call, show = await cinema_press('next', 1, 0)
    assert idx == 0 and call.args == ('🎬 Это единственный фильм',)
    show.assert_not_awaited()


# ---------- админские списки ----------

async def test_admin_hidden_events_and_dedup_report_wrap():
    upd, ctx, q = make_update('admin_hidden_event:prev', {'admin_hidden_event_ids': [1, 2, 3], 'admin_hidden_event_index': 0})
    with patch.object(admin_panel, '_require_admin', AsyncMock(return_value=True)), \
         patch.object(admin_panel, '_show_admin_hidden_event', AsyncMock()):
        await admin_panel.handle_admin_hidden_event_nav(upd, ctx)
    assert ctx.user_data['admin_hidden_event_index'] == 2 and q.answer.await_args.args == ('🔁 К концу списка',)
    upd, ctx, q = make_update('dd:nav:next', {'dd_ids': [1, 2], 'dd_idx': 1})
    with patch.object(admin_dedup_ai, '_require_admin', AsyncMock(return_value=True)), \
         patch.object(admin_dedup_ai, '_show', AsyncMock()):
        await admin_dedup_ai.handle_dd_nav(upd, ctx)
    assert ctx.user_data['dd_idx'] == 0 and q.answer.await_args.args == ('🔁 К началу списка',)
