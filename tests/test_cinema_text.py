"""Тексты киноафиши (чистые функции) и ссылки на карточки."""
from datetime import date, datetime, timedelta

import pytest

from bot.utils import cinema_text as ct
from bot.utils.links import cinema_card_url, parse_cinema_payload
from shared.tz import BELGRADE_TZ


def movie(**kw):
    base = dict(id=1, title_serbian='HOKUS 3D (sinhro.)', title_original='', title_en='', title_ru='', genres_ru=[],
                description_src='', description_ru='', sessions=3)
    base.update(kw)
    return base


@pytest.mark.parametrize('raw,expected', [
    ('HOKUS: MAGIČNO KRALJEVSTVO 3D (sinhronizovano)', 'Hokus: Magično Kraljevstvo'),
    ('Mačak U Šeširu (sinhro.) – 2D', 'Mačak U Šeširu'),
    ('IGRE GLADI :OSVIT DANA ŽETVE', 'Igre Gladi: Osvit Dana Žetve'),
    ("DON'T LOOK UP", "Don't Look Up"),
    ('3:10 to Yuma', '3:10 to Yuma'),
    ('Dina: Treći Deo', 'Dina: Treći Deo'),
    ('', ''), (None, ''),
])
def test_clean_title(raw, expected):
    assert ct.clean_title(raw) == expected


def test_display_title_prefers_english_then_cleaned_serbian():
    assert ct.display_title(movie(title_en='Hocus Pocus')) == 'Hocus Pocus'
    assert ct.display_title(movie()) == 'Hokus'
    assert ct.display_title(movie(title_serbian='', title_original='')) == 'Без названия'


def test_ru_title_hidden_when_empty_or_same():
    assert ct.ru_title(movie(title_en='Dune', title_ru='')) == ''
    assert ct.ru_title(movie(title_en='Dune', title_ru='dune')) == ''
    assert ct.ru_title(movie(title_en='Dune', title_ru='Дюна')) == 'Дюна'


def test_list_line_one_genre_and_sessions():
    m = movie(title_en='Dune', genres_ru=['фантастика', 'драма'], sessions=7)
    assert ct.list_line(m, 40) == '🎬 Dune · фантастика · 7 сеансов'
    assert ct.list_line(movie(title_en='Dune', sessions=1), 40) == '🎬 Dune · 1 сеанс'
    assert ct.list_line(movie(title_en='Dune', sessions=22), 40).endswith('22 сеанса'.replace('22 сеанса', '22 сеанса'))


@pytest.mark.parametrize('n,expected', [(1, '1 сеанс'), (2, '2 сеанса'), (5, '5 сеансов'), (11, '11 сеансов'), (21, '21 сеанс'), (112, '112 сеансов')])
def test_plural_sessions(n, expected):
    assert ct.plural_sessions(n) == expected


def test_sort_is_alphabetical_and_stable():
    films = [movie(id=3, title_en='b'), movie(id=2, title_en='A'), movie(id=1, title_en='a')]
    assert [f['id'] for f in ct.sort_films(films)] == [1, 2, 3]


def test_paging():
    assert ct.page_count(0, 15) == 1 and ct.page_count(15, 15) == 1 and ct.page_count(16, 15) == 2
    assert ct.clamp_page(0, 3) == 1 and ct.clamp_page(9, 3) == 1 and ct.clamp_page(2, 3) == 2
    assert ct.page_of(0, 15) == 1 and ct.page_of(14, 15) == 1 and ct.page_of(15, 15) == 2


def test_empty_text():
    today = date(2026, 11, 26)
    assert ct.empty_text(today, today) == 'На сегодня сеансов нет 🤷'
    assert ct.empty_text(today + timedelta(days=1), today) == 'На завтра сеансов нет 🤷'
    assert 'сеансов нет' in ct.empty_text(date(2026, 12, 5), today)


def test_day_bounds_today_starts_now_and_past_day_is_empty():
    now = datetime(2026, 11, 26, 15, 0, tzinfo=BELGRADE_TZ)
    s, e = ct.day_bounds(date(2026, 11, 26), now)
    assert s == now and e == datetime(2026, 11, 27, tzinfo=BELGRADE_TZ)
    s, e = ct.day_bounds(date(2026, 11, 27), now)
    assert s == datetime(2026, 11, 27, tzinfo=BELGRADE_TZ)
    s, e = ct.day_bounds(date(2026, 11, 25), now)
    assert s == e


def test_description_preview_uses_translation_and_word_boundary():
    long_ru = 'слово ' * 100
    m = movie(description_src='original text', description_ru=long_ru)
    pv = ct.preview_text(m)
    assert len(pv) <= ct.PREVIEW_CHARS + 3 and not pv.endswith(' ') and pv.startswith('слово')
    assert ct.has_more_description(m)
    assert ct.preview_text(movie(description_src='only original')) == 'only original'
    assert not ct.has_more_description(movie(description_src='short'))
    assert not ct.has_more_description(movie())


def test_card_text_escapes_html_and_shows_blocks():
    m = movie(title_en='Tom & Jerry <3', title_ru='Том и Джерри', genres_ru=['комедия', 'семейный'], description_ru='Описание <b>')
    t = ct.card_text(m)
    assert '<b>Tom &amp; Jerry &lt;3</b>' in t
    assert 'Том и Джерри' in t and '🎭 Комедия, семейный' in t and 'Описание &lt;b&gt;' in t


def test_full_text_capped():
    assert len(ct.full_text(movie(description_ru='а ' * 5000))) <= ct.FULL_MAX_CHARS + 3
    assert 'Описание недоступно' in ct.description_screen_text(movie(title_en='X'))


def test_cinema_links_roundtrip():
    url = cinema_card_url('@MyBot', 17, date(2026, 11, 27))
    assert url == 'https://t.me/MyBot?start=cm_17_20261127'
    assert parse_cinema_payload('cm_17_20261127') == (17, date(2026, 11, 27))
    for bad in ['cm_17_20261399', 'cm_x_20261127', 'cw_17_20261127', '', None, 'cm_17']:
        assert parse_cinema_payload(bad) is None
