"""Окно категории (режим window): строки-ссылки, страницы по 15, листание по кругу, переходы и откаты (без БД)."""
from datetime import date, time, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.handlers import categories as cats
from bot.handlers import category_window as cw
from bot.handlers import start as start_mod
from bot.keyboards.event_nav import get_event_nav_keyboard
from bot.utils import links
from bot.utils import window as w
from db import settings
from shared.config import config

TODAY = date(2026, 11, 18)
ST = w.WindowSettings()
ST_VENUE = w.WindowSettings(show_venue=True)


def ev(i, title='Концерт', day=TODAY, venue=None, tm=None):
    return w.Item('e', i, day, title, venue, tm)


def post(i, text, day=TODAY):
    return w.Item('m', i, day, text)


def many(n, day=TODAY):
    return [ev(i, f'Событие {i}', day) for i in range(1, n + 1)]


def cbs(kb):
    return [b.callback_data for r in kb.inline_keyboard for b in r]


def rows(kb):
    return [[b.callback_data for b in r] for r in kb.inline_keyboard]


def day_row_cbs(kb):
    return [b.callback_data for r in kb.inline_keyboard for b in r if b.callback_data.startswith(('catd:', 'catcal:', 'catnoop'))][:3]


def day_row_texts(kb):
    return [b.text for r in kb.inline_keyboard for b in r if b.callback_data.startswith(('catd:', 'catcal:', 'catnoop'))][:3]


# ---------- строки ----------

def test_event_line_with_time_and_venue_cuts_address():
    it = ev(1, 'Dubioza Kolektiv', venue='Štark Arena, Bulevar Arsenija Čarnojevića 58', tm=time(21, 0))
    assert w.build_line(1, it, ST_VENUE) == '• Dubioza Kolektiv · 21:00 · Štark Arena'


def test_venue_is_not_shown_by_default():
    it = ev(1, 'Dubioza Kolektiv', venue='Štark Arena, Bulevar 58', tm=time(21, 0))
    assert w.build_line(1, it, ST) == '• Dubioza Kolektiv · 21:00'
    assert w.build_line(1, ev(2, 'Без места', venue='Клуб'), ST) == '• Без места'


@pytest.mark.parametrize('raw,expected', [(True, True), (False, False), ('true', True), ('Off', False), ('мусор', False), (None, False), (1, True)])
def test_show_venue_setting_parsing(monkeypatch, raw, expected):
    monkeypatch.setitem(config, 'ui', {'window': {'show_venue': raw}})
    assert w.window_settings().show_venue is expected


def test_show_venue_absent_in_config_means_hidden(monkeypatch):
    monkeypatch.setitem(config, 'ui', {})
    assert w.window_settings().show_venue is False


def test_event_line_without_time_and_venue():
    assert w.build_line(7, ev(1, 'Джазовый вечер'), ST) == '• Джазовый вечер'


def test_long_title_truncated_on_word_with_ellipsis():
    line = w.build_line(1, ev(1, 'Очень длинное название концерта которое не помещается в одну строку на телефоне'), ST)
    assert line.endswith('…') and len(line) <= 3 + ST.title_max


def test_post_title_skips_links_tags_and_emoji():
    text = '🎶🎶 \n#концерт @kanal https://t.me/x/1\n🔥 *Jazz night* в Бунтe\nПодробности внутри'
    assert w.post_title(text) == 'Jazz night в Бунтe'


def test_post_title_empty_text_gives_placeholder():
    assert w.build_line(1, post(5, '   \n https://x.y'), ST) == '• Без названия'
    assert w.build_line(1, post(5, None), ST) == '• Без названия'


def test_post_title_skips_day_header_and_takes_what_is_under_it():
    """Реальный пост: «Среда, 18 ноября» и под ним «• Vojkan Bećir / 20/44, 23:00» (скриншот из бота)."""
    text = 'Среда, 18 ноября\n• Vojkan Bećir / 20/44, 23:00'
    assert w.post_title(text) == 'Vojkan Bećir / 20/44, 23:00'
    assert w.build_line(2, post(5, text), ST) == '• Vojkan Bećir / 20/44, 23:00'


@pytest.mark.parametrize('header', ['Среда', 'Среда, 18 ноября', '📅 Среда, 18 ноября:', '*Ср 18.11*', '18 ноября', '18.11.2026',
                                    'Сегодня, 18 ноября', 'Пт, 20.11 —', '20 ноября, пятница', '18/11', 'СРЕДА'])
def test_day_headers_are_skipped(header):
    assert w.post_title(f'{header}\nДжаз-вечер в Бунте') == 'Джаз-вечер в Бунте'


@pytest.mark.parametrize('title', ['Пятница 13-го: вечеринка', 'Концерт 20 ноября в Бунте', 'Среда обитания: встреча',
                                   '18 ноября: стендап', '20:00 Джаз', 'Вторник в Беломорье'])
def test_real_titles_with_dates_or_weekdays_are_not_mistaken_for_headers(title):
    assert w.post_title(title) == title


def test_post_with_only_a_header_falls_back_to_the_header():
    assert w.post_title('🔥 Среда, 18 ноября\n\n') == 'Среда, 18 ноября'
    assert w.post_title('Среда, 18 ноября\n#tag https://t.me/x') == 'Среда, 18 ноября'


def test_two_headers_in_a_row_are_both_skipped():
    assert w.post_title('Афиша\nСреда\n18 ноября\nКонцерт в Бунте') == 'Афиша'


def test_post_line_has_no_venue_even_if_present():
    assert w.build_line(1, w.Item('m', 5, TODAY, 'Вечеринка в клубе', venue='Клуб X'), ST) == '• Вечеринка в клубе'


def test_settings_from_config_with_garbage_and_clamps(monkeypatch):
    monkeypatch.setitem(config, 'ui', {'window': {'page_size': 'abc', 'title_max': 30, 'venue_max': 9999, 'period_list_max': 1}})
    st = w.window_settings()
    assert st.page_size == 15 and st.title_max == 30 and st.venue_max == 40 and st.period_list_max == 10


def test_worst_case_page_always_fits_telegram_limit():
    """Верхние границы настроек: самая длинная страница (20 строк по 80+40 символов, заголовки дней) < 4096."""
    st = w.WindowSettings(page_size=20, title_max=80, venue_max=40)
    items = [ev(i, 'ж' * 200, TODAY + timedelta(days=i), 'м' * 100, time(20, 0)) for i in range(20)]
    lay = w.layout_window(items, 'H' * 50, TODAY, TODAY + timedelta(days=30), TODAY, w.WindowSettings(**{**st.__dict__, 'period_list_max': 200}))
    html, _ = w.render_text(lay, lambda it: 'https://t.me/bot?start=cw_concerts_20261118_20261218_e1234567')
    plain, _ = w.render_text(lay)
    assert lay.mode == 'list' and len(plain) < 4096


# ---------- раскладка и страницы ----------

def test_empty_layout():
    assert w.layout_window([], 'H', TODAY, TODAY, TODAY, ST).mode == 'empty'


def test_up_to_fifteen_is_one_page_without_pager_text():
    lay = w.layout_window(many(15), 'H', TODAY, TODAY, TODAY, ST)
    assert lay.pages == 1 and len(lay.shown) == 15 and 'стр.' not in lay.head


def test_sixteen_events_make_two_pages():
    items = many(16)
    p1 = w.layout_window(items, 'H', TODAY, TODAY, TODAY, ST, page=1)
    p2 = w.layout_window(items, 'H', TODAY, TODAY, TODAY, ST, page=2)
    assert (p1.pages, len(p1.shown), len(p2.shown)) == (2, 15, 1)
    assert 'стр. 1/2' in p1.head and 'стр. 2/2' in p2.head and '16 событий' in p2.head
    assert [r.number for r in p2.rows if r.kind == 'item'] == [16]                # нумерация сквозная


@pytest.mark.parametrize('page', [0, -3, 99])
def test_bad_page_falls_back_to_first(page):
    lay = w.layout_window(many(40), 'H', TODAY, TODAY, TODAY, ST, page=page)
    assert lay.page == 1 and lay.shown[0].id == 1


def test_period_list_has_day_headers_inside_pages_and_continuous_numbers():
    d1 = TODAY + timedelta(days=1)
    items = [ev(1, 'А', TODAY), ev(2, 'Б', TODAY), ev(3, 'В', d1)]
    lay = w.layout_window(items, 'H', TODAY, d1, TODAY, ST)
    kinds = [(r.kind, r.text if r.kind == 'day' else r.number) for r in lay.rows]
    assert kinds == [('day', 'Ср, 18 ноября'), ('item', 1), ('item', 2), ('day', 'Чт, 19 ноября'), ('item', 3)]


def test_period_page_two_starts_with_its_day_header():
    items = [ev(i, f'E{i}', TODAY + timedelta(days=i // 10)) for i in range(30)]        # по 10 в день, 3 дня
    lay = w.layout_window(items, 'H', TODAY, TODAY + timedelta(days=2), TODAY, ST, page=2)
    assert lay.rows[0].kind == 'day' and lay.rows[1].number == 16


def test_big_period_becomes_aggregate_by_days():
    items = [ev(i, f'E{i}', TODAY + timedelta(days=i // 30)) for i in range(90)]
    lay = w.layout_window(items, 'H', TODAY, TODAY + timedelta(days=2), TODAY, ST)
    assert lay.mode == 'aggregate' and lay.shown == [] and [c for _, c in lay.day_counts] == [30, 30, 30]
    text, mode = w.render_text(lay)
    assert '30 событий' in text and '• ' not in text and mode is None


def test_period_with_single_active_day_is_a_normal_day_list():
    lay = w.layout_window([ev(1, 'А'), ev(2, 'Б')], 'H', TODAY, TODAY + timedelta(days=3), TODAY, ST)
    assert lay.mode == 'list' and all(r.kind == 'item' for r in lay.rows)


def test_year_shown_only_when_not_current():
    assert w.format_day_title(date(2027, 1, 5), TODAY) == 'Вт, 5 января 2027'
    assert w.format_day_title(date(2026, 12, 5), TODAY) == 'Сб, 5 декабря'


# ---------- текст со ссылками ----------

def url_for(it):
    return links.window_card_url('TestBot', 'concerts', TODAY, TODAY, it.ref)


def test_every_line_is_a_link_to_its_card_html_escaped():
    items = [ev(10, 'Rock & Roll <live>', venue='Клуб', tm=time(21, 0)), post(77, '🎶 Пост про джаз')]
    lay = w.layout_window(items, '🎵 Концерты · Ср, 18 ноября', TODAY, TODAY, TODAY, ST_VENUE)
    text, mode = w.render_text(lay, url_for)
    assert mode == 'HTML'
    assert '• <a href="https://t.me/TestBot?start=cw_concerts_20261118_20261118_e10">Rock &amp; Roll &lt;live&gt; · 21:00 · Клуб</a>' in text
    assert '• <a href="https://t.me/TestBot?start=cw_concerts_20261118_20261118_77">Пост про джаз</a>' in text
    assert text.startswith('🎵 Концерты · Ср, 18 ноября · 2 события\n\n• ')


def test_day_headers_are_bold_in_html_and_plain_without_links():
    d1 = TODAY + timedelta(days=1)
    lay = w.layout_window([ev(1, 'А'), ev(2, 'Б', d1)], 'H', TODAY, d1, TODAY, ST)
    html, _ = w.render_text(lay, url_for)
    plain, mode = w.render_text(lay)
    assert '<b>Ср, 18 ноября</b>' in html and '<b>Чт, 19 ноября</b>' in html
    assert mode is None and '<' not in plain and 'Ср, 18 ноября\n• А' in plain


# ---------- ссылки /start ----------

def test_window_link_roundtrip_and_length():
    url = links.window_card_url('@TestBot', 'festival', date(2026, 11, 18), date(2026, 12, 8), 'e1234567')
    assert url == 'https://t.me/TestBot?start=cw_festival_20261118_20261208_e1234567'
    payload = url.split('start=')[1]
    assert len(payload) <= 64
    assert links.parse_window_payload(payload) == ('festival', date(2026, 11, 18), date(2026, 12, 8), 'e1234567')
    assert links.parse_window_payload('cw_concerts_20261118_20261118_123456789012') == ('concerts', TODAY, TODAY, 123456789012)


@pytest.mark.parametrize('raw', [None, '', 'fav_12', 'cw_concerts_20261118_20261118_', 'cw_concerts_2026118_20261118_e1',
                                 'cw_concerts_20261318_20261118_e1', 'cw_Concerts_20261118_20261118_e1', 'cw_concerts_20261118_20261118_x5'])
def test_window_payload_rejects_garbage(raw):
    assert links.parse_window_payload(raw) is None


# ---------- клавиатура ----------

def test_no_buttons_lead_to_events():
    lay = w.layout_window(many(40), 'H', TODAY, TODAY, TODAY, ST, page=2)
    kb = cw.build_keyboard('concerts', TODAY, TODAY, lay)
    assert not any(c.startswith(('wo:', 'wc:')) for c in cbs(kb))
    assert all(len(c.encode()) <= 64 for c in cbs(kb))


def test_pager_row_only_when_more_than_one_page():
    kb1 = cw.build_keyboard('concerts', TODAY, TODAY, w.layout_window(many(15), 'H', TODAY, TODAY, TODAY, ST))
    assert not any(c.startswith('wl:') for c in cbs(kb1))
    kb2 = cw.build_keyboard('concerts', TODAY, TODAY, w.layout_window(many(16), 'H', TODAY, TODAY, TODAY, ST))
    assert rows(kb2)[0] == ['wl:concerts:2026-11-18:2026-11-18:2', 'catnoop', 'wl:concerts:2026-11-18:2026-11-18:2']
    assert kb2.inline_keyboard[0][1].text == '1/2'


def test_pager_wraps_around_both_ways():
    items = many(40)                                                              # 3 страницы
    first = cw.build_keyboard('concerts', TODAY, TODAY, w.layout_window(items, 'H', TODAY, TODAY, TODAY, ST, page=1))
    mid = cw.build_keyboard('concerts', TODAY, TODAY, w.layout_window(items, 'H', TODAY, TODAY, TODAY, ST, page=2))
    last = cw.build_keyboard('concerts', TODAY, TODAY, w.layout_window(items, 'H', TODAY, TODAY, TODAY, ST, page=3))
    base = 'wl:concerts:2026-11-18:2026-11-18:'
    assert (rows(first)[0][0], rows(first)[0][2]) == (base + '3', base + '2')
    assert (rows(mid)[0][0], rows(mid)[0][2]) == (base + '1', base + '3')
    assert (rows(last)[0][0], rows(last)[0][2]) == (base + '2', base + '1')


def test_navigation_rows_and_back_target():
    lay = w.layout_window([ev(1, 'А')], 'H', TODAY, TODAY, TODAY, ST)
    assert rows(cw.build_keyboard('festival', TODAY, TODAY, lay))[-2:] == [['catnoop', 'catd:festival:tomorrow', 'catcal:festival'], ['cats:menu']]
    assert rows(cw.build_keyboard('concerts', TODAY, TODAY, lay))[-1] == ['nav:menu']


def test_aggregate_has_day_buttons_three_per_row():
    items = [ev(i, f'E{i}', TODAY + timedelta(days=i // 30)) for i in range(150)]
    last = TODAY + timedelta(days=4)
    kb = cw.build_keyboard('concerts', TODAY, last, w.layout_window(items, 'H', TODAY, last, TODAY, ST))
    assert rows(kb)[0] == ['wl:concerts:2026-11-18:2026-11-18', 'wl:concerts:2026-11-19:2026-11-19', 'wl:concerts:2026-11-20:2026-11-20']
    assert kb.inline_keyboard[0][0].text == 'Ср 18.11 · 30'


def test_active_day_button_is_inactive_so_repress_does_not_duplicate_message():
    lay = w.layout_window([ev(1, 'А')], 'H', TODAY, TODAY, TODAY, ST)
    assert day_row_cbs(cw.build_keyboard('concerts', TODAY, TODAY, lay)) == ['catnoop', 'catd:concerts:tomorrow', 'catcal:concerts']
    t1 = TODAY + timedelta(days=1)
    lay = w.layout_window([ev(1, 'А', t1)], 'H', t1, t1, TODAY, ST)
    assert day_row_cbs(cw.build_keyboard('concerts', t1, t1, lay)) == ['catd:concerts:today', 'catnoop', 'catcal:concerts']
    last = TODAY + timedelta(days=3)
    lay = w.layout_window([ev(1, 'А'), ev(2, 'Б', t1)], 'H', TODAY, last, TODAY, ST)
    assert day_row_cbs(cw.build_keyboard('concerts', TODAY, last, lay)) == ['catd:concerts:today', 'catd:concerts:tomorrow', 'catcal:concerts']


def test_catnoop_is_handled_by_registered_pattern():
    import re
    assert "catnoop(:prev|:next)?" in open('bot/main.py', encoding='utf-8').read() and re.match(r'^(catnoop(:prev|:next)?|calendar:ignore)$', 'catnoop')


def test_wl_handler_is_registered_and_old_event_buttons_are_gone():
    src = open('bot/main.py', encoding='utf-8').read()
    assert "pattern='^wl:'" in src and "pattern='^wo:'" not in src and "pattern='^wc:'" not in src


# ---------- тексты ----------

def test_empty_texts():
    assert cw.empty_text(TODAY, TODAY, TODAY) == 'На сегодня ничего нет 🤷'
    assert cw.empty_text(TODAY + timedelta(days=1), TODAY + timedelta(days=1), TODAY) == 'На завтра ничего нет 🤷'
    assert 'Сб, 21 ноября' in cw.empty_text(date(2026, 11, 21), date(2026, 11, 21), TODAY)
    assert cw.empty_text(TODAY, TODAY + timedelta(days=2), TODAY) == 'В этом периоде ничего нет 🤷'


def test_parse_cat_filter():
    assert cw.parse_cat_filter('cat:concerts:date:2026-11-20') == ('concerts', date(2026, 11, 20), date(2026, 11, 20))
    assert cw.parse_cat_filter('cat:concerts:range:2026-11-20:2026-11-22') == ('concerts', date(2026, 11, 20), date(2026, 11, 22))
    assert cw.parse_cat_filter('cat:concerts') is None and cw.parse_cat_filter('favorites') is None
    assert cw.parse_cat_filter('cat:concerts:date:bad') is None


# ---------- обработчики ----------

def make_update(data='x', user_id=1):
    q = MagicMock(); q.data = data; q.answer = AsyncMock()
    upd = MagicMock(callback_query=q); upd.effective_user.id = user_id; upd.effective_chat.id = 1
    upd.message.message_id = 4242
    ctx = MagicMock(); ctx.user_data = {}; ctx.bot_data = {'pool': MagicMock()}; ctx.bot.send_message = AsyncMock(); ctx.args = []
    return upd, ctx, q


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    settings.clear_cache()
    monkeypatch.setitem(config['telegram'], 'admin_user_id', 999)
    monkeypatch.setattr(cw, 'belgrade_today', lambda: TODAY)
    monkeypatch.setattr(cats, 'belgrade_today', lambda: TODAY)
    monkeypatch.setattr(cw, 'bot_username', AsyncMock(return_value='TestBot'))
    monkeypatch.setattr(cw, '_last_admin_alert', 0.0)
    yield
    settings.clear_cache()


def mode(monkeypatch, value):
    monkeypatch.setattr(cats, 'resolve_category_mode', AsyncMock(return_value=value))


async def test_window_mode_day_button_renders_linked_list_not_cards(monkeypatch):
    mode(monkeypatch, 'window')
    monkeypatch.setitem(config, 'ui', {'window': {'show_venue': True}})
    sent = AsyncMock(); monkeypatch.setattr(cw, 'edit_or_send', sent)
    monkeypatch.setattr(cw, 'load_items', AsyncMock(return_value=[ev(1, 'Концерт А', venue='Клуб')]))
    upd, ctx, q = make_update('catd:concerts:today')
    with patch.object(cats, 'show_current_event', AsyncMock()) as show, patch('bot.handlers.events.load_event_ids', AsyncMock()) as load:
        await cats.handle_category_day(upd, ctx)
    show.assert_not_awaited(); load.assert_not_awaited()
    kw = sent.await_args.kwargs
    assert kw['parse_mode'] == 'HTML' and kw['disable_link_preview'] is True
    assert kw['text'].startswith('🎵 Концерты · Ср, 18 ноября · 1 событие')
    assert '• <a href="https://t.me/TestBot?start=cw_concerts_20261118_20261118_e1">Концерт А · Клуб</a>' in kw['text']
    q.answer.assert_awaited_once_with()


async def test_window_without_bot_username_falls_back_to_plain_lines(monkeypatch):
    mode(monkeypatch, 'window')
    monkeypatch.setattr(cw, 'bot_username', AsyncMock(return_value=None))
    sent = AsyncMock(); monkeypatch.setattr(cw, 'edit_or_send', sent)
    monkeypatch.setattr(cw, 'load_items', AsyncMock(return_value=[ev(1, 'Концерт А')]))
    upd, ctx, q = make_update('catd:concerts:today')
    await cats.handle_category_day(upd, ctx)
    assert sent.await_args.kwargs['parse_mode'] is None and '• Концерт А' in sent.await_args.kwargs['text']


async def test_window_mode_empty_day_shows_text_and_day_buttons_without_alert(monkeypatch):
    mode(monkeypatch, 'window')
    sent = AsyncMock(); monkeypatch.setattr(cw, 'edit_or_send', sent)
    monkeypatch.setattr(cw, 'load_items', AsyncMock(return_value=[]))
    upd, ctx, q = make_update('catd:concerts:today')
    await cats.handle_category_day(upd, ctx)
    assert 'На сегодня ничего нет 🤷' in sent.await_args.kwargs['text']
    assert cbs(sent.await_args.kwargs['keyboard'])[:3] == ['catnoop', 'catd:concerts:tomorrow', 'catcal:concerts']
    assert q.answer.await_args.kwargs.get('show_alert') is None


async def test_cards_mode_unchanged(monkeypatch):
    mode(monkeypatch, 'cards')
    upd, ctx, q = make_update('catd:fun:tomorrow')
    with patch('bot.handlers.events.load_event_ids', AsyncMock(return_value=['e1'])), \
         patch.object(cats, 'show_current_event', AsyncMock()) as show, patch.object(cw, 'load_items', AsyncMock()) as li:
        await cats.handle_category_day(upd, ctx)
    show.assert_awaited_once(); li.assert_not_awaited()


async def test_window_error_falls_back_to_cards_and_alerts_admin(monkeypatch):
    mode(monkeypatch, 'window')
    monkeypatch.setattr(cw, 'load_items', AsyncMock(side_effect=RuntimeError('boom')))
    upd, ctx, q = make_update('catd:concerts:today')
    with patch('bot.handlers.events.load_event_ids', AsyncMock(return_value=['e1'])), patch.object(cats, 'show_current_event', AsyncMock()) as show:
        await cats.handle_category_day(upd, ctx)
    show.assert_awaited_once()
    args = ctx.bot.send_message.await_args.kwargs
    assert args['chat_id'] == 999 and 'RuntimeError' in args['text']


async def test_second_failure_within_pause_does_not_spam_admin(monkeypatch):
    mode(monkeypatch, 'window')
    monkeypatch.setattr(cw, 'load_items', AsyncMock(side_effect=RuntimeError('boom')))
    counts = []
    for _ in range(2):
        upd, ctx, q = make_update('catd:concerts:today')
        with patch('bot.handlers.events.load_event_ids', AsyncMock(return_value=['e1'])), patch.object(cats, 'show_current_event', AsyncMock()):
            await cats.handle_category_day(upd, ctx)
        counts.append(ctx.bot.send_message.await_count)
    assert counts == [1, 0]


async def test_calendar_go_for_period_uses_window_in_window_mode(monkeypatch):
    mode(monkeypatch, 'window')
    sent = AsyncMock(); monkeypatch.setattr(cw, 'edit_or_send', sent)
    monkeypatch.setattr(cw, 'load_items', AsyncMock(return_value=[ev(1, 'А', TODAY), ev(2, 'Б', TODAY + timedelta(days=1))]))
    upd, ctx, q = make_update('catgo:concerts:2026-11-18:2026-11-19')
    await cats.handle_category_go(upd, ctx)
    text = sent.await_args.kwargs['text']
    assert '18–19 ноября' in text and 'Чт, 19 ноября' in text


async def test_page_callback_shows_requested_page(monkeypatch):
    sent = AsyncMock(); monkeypatch.setattr(cw, 'edit_or_send', sent)
    monkeypatch.setattr(cw, 'load_items', AsyncMock(return_value=many(40)))
    upd, ctx, q = make_update('wl:concerts:2026-11-18:2026-11-18:3')
    await cw.handle_window_list(upd, ctx)
    text = sent.await_args.kwargs['text']
    assert 'стр. 3/3' in text and '>Событие 31</a>' in text and '>Событие 40</a>' in text and '>Событие 30</a>' not in text


@pytest.mark.parametrize('data', ['wl:zzz:2026-11-18:2026-11-18', 'wl:concerts:bad:2026-11-18', 'wl:concerts:2026-11-19:2026-11-18',
                                  'wl:concerts:2026-11-18:2026-12-30', 'wl:concerts:2026-11-18:2026-11-18:x', 'wl:concerts:2026-11-18:2026-11-18:0',
                                  'wl:concerts:2026-11-18'])
async def test_list_callback_rejects_bad_data(data):
    upd, ctx, q = make_update(data)
    await cw.handle_window_list(upd, ctx)
    assert q.answer.await_args.kwargs.get('show_alert') is True


async def test_category_press_in_window_mode_opens_list_for_today_directly(monkeypatch):
    mode(monkeypatch, 'window')
    monkeypatch.setitem(config, 'ui', {'window': {'show_venue': True}})
    sent = AsyncMock(); monkeypatch.setattr(cw, 'edit_or_send', sent)
    monkeypatch.setattr(cw, 'load_items', AsyncMock(return_value=[ev(1, 'Концерт А', venue='Клуб', tm=time(21, 0))]))
    monkeypatch.setattr(cats, 'update_user_stats', AsyncMock())
    upd, ctx, q = make_update('cat:concerts')
    with patch.object(cats, '_render_window', AsyncMock()) as day_screen:
        await cats.handle_category(upd, ctx)
    day_screen.assert_not_awaited()
    assert 'Концерт А · 21:00 · Клуб</a>' in sent.await_args.kwargs['text']
    assert day_row_texts(sent.await_args.kwargs['keyboard']) == ['• 📅 Сегодня', '📅 Завтра', '📅 На дату']


async def test_category_press_in_cards_mode_keeps_day_screen(monkeypatch):
    mode(monkeypatch, 'cards')
    monkeypatch.setattr(cats, 'update_user_stats', AsyncMock())
    upd, ctx, q = make_update('cat:concerts')
    with patch.object(cats, '_render_window', AsyncMock()) as day_screen, patch.object(cw, 'load_items', AsyncMock()) as li:
        await cats.handle_category(upd, ctx)
    day_screen.assert_awaited_once(); li.assert_not_awaited()


async def test_category_press_window_error_falls_back_to_day_screen(monkeypatch):
    mode(monkeypatch, 'window')
    monkeypatch.setattr(cw, 'load_items', AsyncMock(side_effect=RuntimeError('boom')))
    monkeypatch.setattr(cats, 'update_user_stats', AsyncMock())
    upd, ctx, q = make_update('cat:concerts')
    with patch.object(cats, '_render_window', AsyncMock()) as day_screen:
        await cats.handle_category(upd, ctx)
    day_screen.assert_awaited_once()


# ---------- ссылка из списка → карточка ----------

async def test_open_window_card_deletes_command_and_list_opens_card_in_same_list(monkeypatch):
    upd, ctx, q = make_update()
    ctx.user_data['menu_message_id'] = 555
    drop = AsyncMock(); open_filter = AsyncMock()
    with patch('bot.handlers.push_links.drop_menu', drop), patch('bot.handlers.events.open_filter', open_filter), \
         patch('bot.handlers.events.load_event_ids', AsyncMock(return_value=['e11', 'e22', 33])):
        ok = await cw.open_window_card(upd, ctx, 'concerts', TODAY, TODAY, 'e22')
    assert ok is True
    assert drop.await_args.args[3] == [4242, 555]                                 # команда /start и сообщение со списком
    open_filter.assert_awaited_once_with(upd, ctx, 'cat:concerts:date:2026-11-18', start_ref='e22')
    assert ctx.user_data['window_filter'] == 'cat:concerts:date:2026-11-18'


async def test_open_window_card_post_ref_is_int():
    upd, ctx, q = make_update()
    open_filter = AsyncMock()
    with patch('bot.handlers.push_links.drop_menu', AsyncMock()), patch('bot.handlers.events.open_filter', open_filter), \
         patch('bot.handlers.events.load_event_ids', AsyncMock(return_value=['e11', 33])):
        assert await cw.open_window_card(upd, ctx, 'concerts', TODAY, TODAY, 33)
    assert open_filter.await_args.kwargs['start_ref'] == 33


async def test_open_window_card_missing_from_fresh_list_shows_the_list(monkeypatch):
    upd, ctx, q = make_update()
    shown = AsyncMock(return_value=True); monkeypatch.setattr(cw, 'try_show_window', shown)
    open_filter = AsyncMock()
    with patch('bot.handlers.push_links.drop_menu', AsyncMock()), patch('bot.handlers.events.open_filter', open_filter), \
         patch('bot.handlers.events.load_event_ids', AsyncMock(return_value=['e11'])):
        assert await cw.open_window_card(upd, ctx, 'concerts', TODAY, TODAY, 'e99') is True
    open_filter.assert_not_awaited(); shown.assert_awaited_once()
    assert 'window_filter' not in ctx.user_data


async def test_open_window_card_rejects_bad_period():
    upd, ctx, q = make_update()
    with patch('bot.handlers.push_links.drop_menu', AsyncMock()) as drop:
        assert await cw.open_window_card(upd, ctx, 'concerts', TODAY, TODAY + timedelta(days=90), 'e1') is False
        assert await cw.open_window_card(upd, ctx, 'nope', TODAY, TODAY, 'e1') is False
    drop.assert_not_awaited()


async def test_start_with_window_payload_opens_card(monkeypatch):
    upd, ctx, q = make_update()
    ctx.args = ['cw_concerts_20261118_20261118_e22']
    monkeypatch.setattr(start_mod, 'update_user_stats', AsyncMock())
    opened = AsyncMock(return_value=True); monkeypatch.setattr(start_mod, 'open_window_card', opened)
    menu = AsyncMock(); monkeypatch.setattr(start_mod, 'show_main_menu', menu)
    await start_mod.handle_start(upd, ctx)
    opened.assert_awaited_once_with(upd, ctx, 'concerts', TODAY, TODAY, 'e22')
    menu.assert_not_awaited()


async def test_start_window_link_failure_falls_back_to_main_menu(monkeypatch):
    upd, ctx, q = make_update()
    ctx.args = ['cw_concerts_20261118_20261118_e22']
    monkeypatch.setattr(start_mod, 'update_user_stats', AsyncMock())
    monkeypatch.setattr(start_mod, 'open_window_card', AsyncMock(side_effect=RuntimeError('db')))
    menu = AsyncMock(); monkeypatch.setattr(start_mod, 'show_main_menu', menu)
    await start_mod.handle_start(upd, ctx)
    menu.assert_awaited_once()


async def test_plain_start_and_fav_payload_unaffected(monkeypatch):
    upd, ctx, q = make_update()
    monkeypatch.setattr(start_mod, 'update_user_stats', AsyncMock())
    opened = AsyncMock(); monkeypatch.setattr(start_mod, 'open_window_card', opened)
    menu = AsyncMock(); monkeypatch.setattr(start_mod, 'show_main_menu', menu)
    await start_mod.handle_start(upd, ctx)
    opened.assert_not_awaited(); menu.assert_awaited_once()


# ---------- «К списку» ----------

def card_kb(window_filter, current_filter='cat:concerts:date:2026-11-18', index=0):
    from bot.handlers.events import _list_back_data
    ctx = MagicMock(); ctx.user_data = {'window_filter': window_filter, 'current_index': index}
    return get_event_nav_keyboard(index, 40, 'e1', False, category_code='concerts', calendar_back='catback:concerts:2026-11-18:2026-11-18',
                                  list_back=_list_back_data(ctx, current_filter))


def test_card_back_to_list_goes_to_the_page_of_the_card():
    assert 'wl:concerts:2026-11-18:2026-11-18:1' in cbs(card_kb('cat:concerts:date:2026-11-18', index=0))
    assert 'wl:concerts:2026-11-18:2026-11-18:1' in cbs(card_kb('cat:concerts:date:2026-11-18', index=14))
    assert 'wl:concerts:2026-11-18:2026-11-18:2' in cbs(card_kb('cat:concerts:date:2026-11-18', index=15))
    assert 'wl:concerts:2026-11-18:2026-11-18:3' in cbs(card_kb('cat:concerts:date:2026-11-18', index=39))


def test_card_has_back_to_list_only_when_opened_from_window():
    assert not any(c.startswith('wl:') for c in cbs(card_kb(None)))
    assert not any(c.startswith('wl:') for c in cbs(card_kb('cat:concerts:date:2026-11-19')))


def test_card_list_button_for_period():
    cur = 'cat:concerts:range:2026-11-18:2026-11-20'
    assert 'wl:concerts:2026-11-18:2026-11-20:1' in cbs(card_kb(cur, cur))


async def test_cards_path_clears_window_marker(monkeypatch):
    mode(monkeypatch, 'cards')
    upd, ctx, q = make_update('catd:fun:today')
    ctx.user_data['window_filter'] = 'cat:fun:date:2026-11-18'
    with patch('bot.handlers.events.load_event_ids', AsyncMock(return_value=['e1'])), patch.object(cats, 'show_current_event', AsyncMock()):
        await cats.handle_category_day(upd, ctx)
    assert 'window_filter' not in ctx.user_data


async def test_window_hides_venue_by_default_in_the_message(monkeypatch):
    mode(monkeypatch, 'window')
    monkeypatch.setitem(config, 'ui', {})
    sent = AsyncMock(); monkeypatch.setattr(cw, 'edit_or_send', sent)
    monkeypatch.setattr(cw, 'load_items', AsyncMock(return_value=[ev(1, 'Концерт А', venue='Клуб', tm=time(21, 0))]))
    upd, ctx, q = make_update('catd:concerts:today')
    await cats.handle_category_day(upd, ctx)
    text = sent.await_args.kwargs['text']
    assert 'Концерт А · 21:00</a>' in text and 'Клуб' not in text
