"""Календарь категории: выбор дня или периода в пределах месяца, сводка, кнопка «К календарю» (без БД)."""
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.handlers import categories as cats
from bot.handlers.calendar import (
    MONTHS_RU, build_range_calendar, format_day_ru, format_period_ru, plural_events,
)
from bot.handlers.events import _calendar_back_data, load_event_ids
from bot.keyboards.event_nav import get_event_nav_keyboard
from bot.utils.categories import feed_sql, period_count_sql

TODAY = date.today()
FIRST = TODAY.replace(day=1)
NEXT_MONTH = (FIRST + timedelta(days=32)).replace(day=1)


def flat(rows):
    return [b for r in rows for b in r]


def day_buttons(rows):
    return {b.text: b for b in flat(rows) if b.text.isdigit()}


def make_update(data):
    q = MagicMock(); q.data = data; q.answer = AsyncMock()
    upd = MagicMock(callback_query=q)
    upd.effective_user.id = 1; upd.effective_chat.id = 1
    ctx = MagicMock(); ctx.user_data = {}; ctx.bot_data = {'pool': MagicMock()}
    return upd, ctx, q


# ---------- форматирование ----------

def test_period_and_day_formatting():
    assert format_day_ru(date(2026, 11, 19)) == '19 ноября'
    assert format_period_ru(date(2026, 11, 19), date(2026, 11, 22)) == '19–22 ноября'
    assert format_period_ru(date(2026, 11, 19), date(2026, 11, 19)) == '19 ноября'


@pytest.mark.parametrize('n,expected', [(0, '0 событий'), (1, '1 событие'), (2, '2 события'), (5, '5 событий'),
                                       (11, '11 событий'), (12, '12 событий'), (21, '21 событие'), (23, '23 события'),
                                       (111, '111 событий')])
def test_plural_events(n, expected):
    assert plural_events(n) == expected


# ---------- календарь ----------

def _month(year_month_first):
    return year_month_first.year, year_month_first.month


def _active(days):
    return {FIRST.replace(day=d) for d in days}


def test_nothing_selected_active_days_start_selection_and_arrows_work():
    y, m = _month(FIRST)
    far = date.today() + timedelta(days=60)
    rows = build_range_calendar(y, m, _active([TODAY.day]), far, 'concerts')
    btn = day_buttons(rows)[str(TODAY.day)]
    assert btn.callback_data == f'catpick:concerts:{TODAY.isoformat()}'
    assert getattr(btn, 'style', None) is None
    assert [b.text for b in rows[0]][1] == f'{MONTHS_RU[m]} {y}' and [b.text for b in rows[0]][::2] == ['◀️', '▶️']
    assert rows[0][0].callback_data == 'catnoop:prev' and rows[0][2].callback_data == 'catnoop:next'   # месяц один: подсказка

def test_start_selected_days_carry_start_blue_start_and_arrows_off():
    y, m = _month(TODAY)
    last_day = (NEXT_MONTH - timedelta(days=1)).day
    active = {TODAY, TODAY.replace(day=last_day)}
    rows = build_range_calendar(y, m, active, TODAY.replace(day=last_day), 'concerts', start=TODAY)
    days = day_buttons(rows)
    assert days[str(TODAY.day)].style == 'primary'
    assert days[str(TODAY.day)].callback_data == f'catpick:concerts:{TODAY.isoformat()}:{TODAY.isoformat()}'
    if last_day != TODAY.day:
        end = days[str(last_day)]
        assert end.callback_data == f'catpick:concerts:{TODAY.replace(day=last_day).isoformat()}:{TODAY.isoformat()}'
        assert getattr(end, 'style', None) is None
    assert not any(b.callback_data.startswith('catcal_nav:') for b in flat(rows))
    assert len(rows) == 2 + len(__import__('calendar').monthcalendar(y, m))      # название месяца, дни недели, недели; стрелок нет
    assert [b.text for b in rows[0]] == [f'{MONTHS_RU[m]} {y}']


def test_selection_has_no_blank_button_row_at_the_bottom():
    y, m = _month(TODAY)
    for st, en in ((TODAY, None), (TODAY, TODAY)):
        rows = build_range_calendar(y, m, {TODAY}, TODAY + timedelta(days=3), 'concerts', start=st, end=en)
        assert not [b for b in rows[-1] if not b.text.strip()] or len(rows[-1]) == 7   # последний ряд — неделя, не пара пустых


def test_period_days_blue_including_days_without_events_and_inert():
    start = FIRST.replace(day=20) if FIRST.replace(day=20) >= TODAY else TODAY
    end = start + timedelta(days=3)
    if (end.year, end.month) != (start.year, start.month):
        start = start.replace(day=1); end = start + timedelta(days=3)
    active = {start, end}
    rows = build_range_calendar(start.year, start.month, active, end + timedelta(days=5), 'fun', start=start, end=end)
    blue = {b.text: b for b in flat(rows) if getattr(b, 'style', None) == 'primary'}
    assert set(blue) == {str(d) for d in range(start.day, end.day + 1)} if start >= TODAY else True
    middle = [b for t, b in blue.items() if int(t) not in (start.day, end.day)]
    assert middle and all(b.callback_data == 'catnoop' for b in middle)      # внутри периода без событий — не нажимаются
    ends = [blue[str(start.day)], blue[str(end.day)]]
    assert all(b.callback_data == f'catpick:fun:{d.isoformat()}' for b, d in zip(ends, (start, end)))   # новый выбор


def test_inactive_and_past_days_are_dots():
    far = TODAY + timedelta(days=40)
    rows = build_range_calendar(TODAY.year, TODAY.month, _active([]), far, 'fun')
    assert not day_buttons(rows)
    assert all(b.callback_data == 'catnoop' for b in flat(rows) if b.text == '·')


def test_all_calendar_callbacks_fit_telegram_limit():
    start = date(2026, 12, 3)
    for code in ('concerts', 'festival', 'museums'):
        for st, en in ((None, None), (start, None), (start, date(2026, 12, 29))):
            rows = build_range_calendar(2026, 12, {start, date(2026, 12, 29)}, date(2027, 1, 20), code, st, en)
            rows.append([cats.InlineKeyboardButton('x', callback_data=f'catgo:{code}:2026-12-03:2026-12-29')])
            rows.append([cats.InlineKeyboardButton('x', callback_data=f'catback:{code}:2026-12-03:2026-12-29')])
            assert all(len(b.callback_data.encode()) <= 64 for b in flat(rows))


# ---------- обработчики ----------

async def test_pick_end_same_month_shows_sorted_period():
    upd, ctx, q = make_update('catpick:concerts:2026-12-10:2026-12-22')
    with patch.object(cats, '_count_period', AsyncMock(return_value=5)), \
         patch.object(cats, '_show_category_calendar', AsyncMock()) as show:
        await cats.handle_category_pick(upd, ctx)
    show.assert_awaited_once_with(upd, ctx, 'concerts', date(2026, 12, 10), date(2026, 12, 22))


async def test_pick_end_before_start_swaps():
    upd, ctx, q = make_update('catpick:concerts:2026-12-03:2026-12-10')
    with patch.object(cats, '_count_period', AsyncMock(return_value=5)), \
         patch.object(cats, '_show_category_calendar', AsyncMock()) as show:
        await cats.handle_category_pick(upd, ctx)
    assert show.await_args.args[3:] == (date(2026, 12, 3), date(2026, 12, 10))


async def test_pick_other_month_starts_new_selection():
    upd, ctx, q = make_update('catpick:concerts:2027-01-05:2026-12-10')
    with patch.object(cats, '_count_period', AsyncMock(return_value=2)), \
         patch.object(cats, '_show_category_calendar', AsyncMock()) as show:
        await cats.handle_category_pick(upd, ctx)
    assert show.await_args.args[3:] == (date(2027, 1, 5),)


async def test_pick_with_nothing_found_alerts_and_keeps_calendar():
    upd, ctx, q = make_update('catpick:concerts:2026-12-10:2026-12-22')
    with patch.object(cats, '_count_period', AsyncMock(return_value=0)), \
         patch.object(cats, '_show_category_calendar', AsyncMock()) as show:
        await cats.handle_category_pick(upd, ctx)
    show.assert_not_awaited()
    assert q.answer.await_args.kwargs.get('show_alert') is True


async def test_go_single_day_and_period_open_matching_filters():
    for data, expected in (('catgo:fun:2026-12-05:2026-12-05', 'cat:fun:date:2026-12-05'),
                           ('catgo:fun:2026-12-05:2026-12-09', 'cat:fun:range:2026-12-05:2026-12-09')):
        upd, ctx, q = make_update(data)
        seen = {}

        async def fake_load(pool, user_id, name):
            seen['filter'] = name
            return ['e1', 'e2']

        with patch('bot.handlers.events.load_event_ids', fake_load), \
             patch.object(cats, 'show_current_event', AsyncMock()) as show:
            await cats.handle_category_go(upd, ctx)
        assert seen['filter'] == expected and ctx.user_data['current_filter'] == expected
        show.assert_awaited_once()


async def test_go_rejects_bad_data_and_empty_result():
    for data in ('catgo:fun:2026-12-09:2026-12-05', 'catgo:zzz:2026-12-05:2026-12-06', 'catgo:fun:oops:2026-12-06', 'catgo:fun:2026-12-05'):
        upd, ctx, q = make_update(data)
        await cats.handle_category_go(upd, ctx)
        assert q.answer.await_args.kwargs.get('show_alert') is True
    upd, ctx, q = make_update('catgo:fun:2026-12-05:2026-12-09')
    with patch('bot.handlers.events.load_event_ids', AsyncMock(return_value=[])), \
         patch.object(cats, 'show_current_event', AsyncMock()) as show:
        await cats.handle_category_go(upd, ctx)
    show.assert_not_awaited()
    assert q.answer.await_args.kwargs.get('show_alert') is True


async def test_reset_shows_calendar_without_selection():
    upd, ctx, q = make_update('catrst:fun')
    with patch.object(cats, '_show_category_calendar', AsyncMock()) as show:
        await cats.handle_category_reset(upd, ctx)
    show.assert_awaited_once_with(upd, ctx, 'fun')


async def test_back_to_calendar_restores_period_but_not_single_day():
    upd, ctx, q = make_update('catback:fun:2026-12-05:2026-12-09')
    with patch.object(cats, '_show_category_calendar', AsyncMock()) as show:
        await cats.handle_category_calendar_back(upd, ctx)
    assert show.await_args.args[2:] == ('fun', date(2026, 12, 5), date(2026, 12, 9))
    assert (ctx.user_data['calendar_year'], ctx.user_data['calendar_month']) == (2026, 12)
    upd, ctx, q = make_update('catback:fun:2026-12-05:2026-12-05')
    with patch.object(cats, '_show_category_calendar', AsyncMock()) as show:
        await cats.handle_category_calendar_back(upd, ctx)
    assert show.await_args.args[2:] == ('fun',)


async def test_calendar_text_has_period_and_count_for_everyone():
    upd, ctx, q = make_update('x')
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value={'dates': [date(2026, 12, 5), date(2026, 12, 9)], 'max_date': date(2026, 12, 30)})
    ctx.bot_data = {'pool': pool}
    with patch.object(cats, '_count_period', AsyncMock(return_value=59)), patch.object(cats, 'edit_or_send', AsyncMock()) as eos:
        await cats._show_category_calendar(upd, ctx, 'fun', date(2026, 12, 5), date(2026, 12, 9))
    kw = eos.await_args.kwargs
    assert '5–9 декабря: 59 событий' in kw['text']
    texts = [b.text for r in kw['keyboard'].inline_keyboard for b in r]
    assert 'Показать 59' in texts and 'Сбросить' in texts
    assert ('catgo:fun:2026-12-05:2026-12-09') in [b.callback_data for r in kw['keyboard'].inline_keyboard for b in r]


# ---------- лента за период ----------

async def test_load_event_ids_period_passes_both_days():
    pool = MagicMock(); pool.fetch = AsyncMock(return_value=[{'kind': 'e', 'id': 7}, {'kind': 'm', 'id': 3}])
    refs = await load_event_ids(pool, 1, 'cat:concerts:range:2026-12-01:2026-12-09')
    assert refs == ['e7', 3]
    args = pool.fetch.await_args.args
    assert args[0] == feed_sql(period=True) and args[-2:] == (date(2026, 12, 1), date(2026, 12, 9))


async def test_load_event_ids_period_bad_inputs_are_empty():
    pool = MagicMock(); pool.fetch = AsyncMock()
    for f in ('cat:concerts:range:2026-12-09:2026-12-01', 'cat:concerts:range:2026-12-01:2027-02-01',
              'cat:concerts:range:garbage:2026-12-01', 'cat:concerts:range:2026-12-01'):
        assert await load_event_ids(pool, 1, f) == []
    pool.fetch.assert_not_awaited()


def test_period_sql_shape():
    sql = feed_sql(period=True)
    assert sql.count('BETWEEN $5::date AND $6::date') == 2
    assert '$6' not in feed_sql(day=True) and '$5' not in feed_sql()
    assert period_count_sql().count('BETWEEN $5::date AND $6::date') == 2


# ---------- кнопка «К календарю» ----------

def test_calendar_back_data_by_filter():
    assert _calendar_back_data('cat:fun:date:2026-12-05') == 'catback:fun:2026-12-05:2026-12-05'
    assert _calendar_back_data('cat:fun:range:2026-12-05:2026-12-09') == 'catback:fun:2026-12-05:2026-12-09'
    assert _calendar_back_data('cat:fun') is None
    assert _calendar_back_data('favorites') is None and _calendar_back_data('') is None


def test_card_keyboard_with_calendar_back_has_calendar_category_and_menu():
    rows = [[(b.text, b.callback_data) for b in r] for r in
            get_event_nav_keyboard(0, 3, 5, False, category_code='fun', calendar_back='catback:fun:2026-12-05:2026-12-09').inline_keyboard]
    assert rows[-2] == [('📅 К календарю', 'catback:fun:2026-12-05:2026-12-09'), ('🗂 К категории', 'cat:fun')]
    assert rows[-1] == [('🏠 В меню', 'nav:menu')]


async def test_noop_buttons_answer_with_hint_on_arrows():
    for data, hint in (('catnoop', None), ('catnoop:prev', 'Других месяцев нет'), ('catnoop:next', 'Других месяцев нет')):
        upd, ctx, q = make_update(data)
        await cats.handle_calendar_noop(upd, ctx)
        q.answer.assert_awaited_once_with(hint)
