"""Новое главное меню, окно категории, кнопка даты и кнопки пересмотра на карточке (без БД)."""
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.handlers import categories as cats
from bot.handlers import navigation
from bot.handlers.events import load_event_ids
from bot.keyboards.event_nav import get_event_nav_keyboard
from bot.keyboards.main_menu import MENU_TEXT, get_main_menu_inline
from bot.utils.categories import MAIN_MENU_CODES, feed_sql, rest_categories


def rows(kb):
    return [[(b.text, b.callback_data) for b in r] for r in kb.inline_keyboard]


def flat_callbacks(kb):
    return [b.callback_data for r in kb.inline_keyboard for b in r]


# ---------- главное меню ----------

def test_main_menu_has_three_rows_and_no_date_buttons():
    kb = get_main_menu_inline(is_admin=False)
    r = rows(kb)
    assert [len(x) for x in r[:4]] == [1, 1, 1, 1]         # четыре кнопки в четыре ряда
    assert [r[i][0][1] for i in range(3)] == ['cat:concerts', 'cat:fun', 'cat:learn']
    assert r[3][0][1] == 'cats:menu' and r[3][0][0].startswith('➕')
    assert [r[4][0][1], r[5][0][1]] == ['cinema:menu', 'filter:favorites']      # афиша кинотеатров, затем запланированное
    assert r[5][0][0].startswith('❤️')
    cb = flat_callbacks(kb)
    for gone in ('filter:today', 'filter:tomorrow', 'filter:week', 'filter:calendar'):
        assert gone not in cb
    assert 'filter:favorites' in cb and 'cinema:menu' in cb


def test_main_menu_cinema_renamed():
    texts = [t for row in rows(get_main_menu_inline()) for t, _ in row]
    assert '🎬 Афиша кинотеатров' in texts
    assert not any('Киноафиша' in t for t in texts)
    assert 'Киноафиша' not in MENU_TEXT and 'Афиша кинотеатров' in MENU_TEXT


def test_main_menu_admin_buttons_kept():
    cb = flat_callbacks(get_main_menu_inline(is_admin=True))
    assert 'admin_panel:main' in cb and 'admin:duplicates' in cb


def test_rest_categories_exclude_main_menu_ones():
    rest = {c.code for c in rest_categories()}
    assert not rest & set(MAIN_MENU_CODES)
    assert {'theater', 'expo', 'family'} <= rest


# ---------- окно категории ----------

def test_window_layout_today_tomorrow_then_date_then_back():
    r = rows(cats.build_window_keyboard('concerts'))
    assert [c for _, c in r[0]] == ['catd:concerts:today', 'catd:concerts:tomorrow']
    assert r[1] == [('📅 На дату', 'catcal:concerts')]
    assert r[2][0][1] == 'nav:menu'                          # главная категория → назад в меню


def test_window_back_for_rest_category_goes_to_rest_list():
    assert rows(cats.build_window_keyboard('theater'))[2][0][1] == 'cats:menu'


def test_rest_menu_keyboard_leads_to_windows():
    from bot.utils.categories import get_category
    kb = cats.build_menu_keyboard([(get_category('theater'), 5)])
    assert rows(kb)[0][0][1] == 'cat:theater' and rows(kb)[-1][0][1] == 'nav:menu'


# ---------- карточка ----------

def test_card_date_button_is_first_and_not_clickable_action():
    kb = get_event_nav_keyboard(0, 3, 'e5', False, event_date=date(2026, 11, 14))
    first = rows(kb)[0]
    assert len(first) == 1 and first[0][0].startswith('📅') and first[0][1] == 'nav:date'
    assert rows(kb)[1][0][1] == 'nav:prev'                   # стрелки сразу под датой


def test_card_without_date_has_no_date_row():
    kb = get_event_nav_keyboard(0, 3, 5, False, event_date=None)
    assert rows(kb)[0][0][1] == 'nav:prev'


def test_card_review_button_only_for_admin():
    user = flat_callbacks(get_event_nav_keyboard(0, 1, 'e5', False, is_admin=False))
    assert not any(c.startswith('rv:') for c in user)
    admin = rows(get_event_nav_keyboard(0, 1, 'e5', False, is_admin=True))
    assert ('🔎 Пересмотр', 'rv:open:e5') in [b for r in admin for b in r]


def test_card_under_review_button_is_labelled_and_inert():
    kb = get_event_nav_keyboard(0, 1, 'e5', False, is_admin=True, under_review=True)
    btns = [b for r in rows(kb) for b in r]
    assert ('⏳ На пересмотре', 'rv:busy') in btns
    assert not any(c == 'rv:open:e5' for _, c in btns)


def test_card_category_and_menu_buttons():
    last = rows(get_event_nav_keyboard(0, 1, 5, False, category_code='concerts'))[-1]
    assert last == [('⬅️ Назад', 'cat:concerts'), ('🏠 В меню', 'nav:menu')]       # лента всей категории: назад в категорию
    assert rows(get_event_nav_keyboard(0, 1, 5, False))[-1] == [('🏠 В меню', 'nav:menu')]


async def test_nav_date_button_just_answers():
    q = MagicMock(); q.data = 'nav:date'; q.answer = AsyncMock()
    upd = MagicMock(callback_query=q)
    ctx = MagicMock(); ctx.user_data = {}; ctx.bot_data = {}
    await navigation.handle_navigation(upd, ctx)
    q.answer.assert_awaited_once_with()


# ---------- ленты ----------

def test_day_feed_sql_filters_one_day_in_both_tables():
    sql = feed_sql(day=True)
    assert sql.count('= $5::date') == 2
    assert '$5' not in feed_sql()


async def test_load_event_ids_category_day_passes_the_day():
    pool = MagicMock(); pool.fetch = AsyncMock(return_value=[{'kind': 'e', 'id': 7}])
    day = date(2026, 12, 1)
    refs = await load_event_ids(pool, 1, f'cat:concerts:date:{day.isoformat()}')
    assert refs == ['e7']
    args = pool.fetch.await_args.args
    assert args[0] == feed_sql(day=True) and args[-1] == day


async def test_load_event_ids_bad_inputs_are_empty():
    pool = MagicMock(); pool.fetch = AsyncMock()
    assert await load_event_ids(pool, 1, 'cat:nope:date:2026-12-01') == []
    assert await load_event_ids(pool, 1, 'cat:concerts:date:garbage') == []
    pool.fetch.assert_not_awaited()


def make_update(data):
    q = MagicMock(); q.data = data; q.answer = AsyncMock()
    upd = MagicMock(callback_query=q)
    upd.effective_user.id = 1; upd.effective_chat.id = 1
    ctx = MagicMock(); ctx.user_data = {}; ctx.bot_data = {'pool': MagicMock()}
    return upd, ctx, q


async def test_day_button_empty_day_shows_alert_and_keeps_window():
    upd, ctx, q = make_update('catd:concerts:today')
    with patch('bot.handlers.events.load_event_ids', AsyncMock(return_value=[])), \
         patch.object(cats, 'show_current_event', AsyncMock()) as show:
        await cats.handle_category_day(upd, ctx)
    show.assert_not_awaited()
    assert q.answer.await_args.kwargs.get('show_alert') is True
    assert 'event_ids' not in ctx.user_data


async def test_day_button_opens_feed_for_that_day():
    upd, ctx, q = make_update('catd:fun:tomorrow')
    seen = {}

    async def fake_load(pool, user_id, name):
        seen['filter'] = name
        return ['e1', 'e2']

    with patch('bot.handlers.events.load_event_ids', fake_load), \
         patch.object(cats, 'show_current_event', AsyncMock()) as show:
        await cats.handle_category_day(upd, ctx)
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    assert seen['filter'] == f'cat:fun:date:{tomorrow}'
    assert ctx.user_data['event_ids'] == ['e1', 'e2'] and ctx.user_data['current_filter'] == seen['filter']
    show.assert_awaited_once()


async def test_pick_from_calendar_selects_start_day():
    upd, ctx, q = make_update('catpick:concerts:2026-12-05')
    with patch.object(cats, '_count_period', AsyncMock(return_value=3)), \
         patch.object(cats, '_show_category_calendar', AsyncMock()) as show:
        await cats.handle_category_pick(upd, ctx)
    show.assert_awaited_once_with(upd, ctx, 'concerts', date(2026, 12, 5))


async def test_pick_same_day_again_opens_that_day_in_same_category():
    upd, ctx, q = make_update('catpick:concerts:2026-12-05:2026-12-05')
    seen = {}

    async def fake_load(pool, user_id, name):
        seen['filter'] = name
        return ['e1']

    with patch('bot.handlers.events.load_event_ids', fake_load), patch.object(cats, 'show_current_event', AsyncMock()):
        await cats.handle_category_pick(upd, ctx)
    assert seen['filter'] == 'cat:concerts:date:2026-12-05'


async def test_bad_callbacks_are_rejected():
    for h, data in ((cats.handle_category_day, 'catd:zzz:today'), (cats.handle_category_day, 'catd:fun:never'),
                    (cats.handle_category_pick, 'catpick:fun:not-a-date'), (cats.handle_category_pick, 'catpick:zzz:2026-12-05')):
        upd, ctx, q = make_update(data)
        await h(upd, ctx)
        assert q.answer.await_args.kwargs.get('show_alert') is True


async def test_category_click_opens_window_not_feed():
    upd, ctx, q = make_update('cat:theater')
    with patch.object(cats, 'edit_or_send', AsyncMock()) as eos, patch.object(cats, 'update_user_stats', AsyncMock()):
        await cats.handle_category(upd, ctx)
    kb = eos.await_args.kwargs['keyboard']
    assert [c for _, c in rows(kb)[0]] == ['catd:theater:today', 'catd:theater:tomorrow']
