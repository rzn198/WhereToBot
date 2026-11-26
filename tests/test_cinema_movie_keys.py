"""Канонический ключ названия фильма: склейка вариантов без AI, без склейки частей и сиквелов."""
import pytest

from cinema_collector.movie_keys import canonical_key
from cinema_collector.saver import movie_key_of, normalize_title
from cinema_collector.scrapers.base import CinemaEvent
from datetime import datetime


def key(title: str) -> str:
    return canonical_key(normalize_title(title))


@pytest.mark.parametrize('a, b', [
    ('3 nedelje posle', 'Tri nedelje posle'),
    ('3 Nedelje Posle', 'TRI NEDELJE POSLE'),
    ('Dina: Drugi deo', 'Dina: Drugi deo Re-release'),
    ('Osvetnici: Uspon Doktora Duma', 'Osvetnici: Uspon Doktora Duma - INFINITY VISION'),
    ('Dina (Re-release)', 'Dina Re-release'),
    ('HOKUS: MAGIČNO KRALJEVSTVO 3D (sinhronizovano)', 'Hokus: Magično Kraljevstvo (sinhro.) – 2D'),
    ('Fokerovi - Stiže snajka', 'FOKEROVI - STIŽE SNAJKA'),
])
def test_variants_of_one_film_share_key(a, b):
    assert key(a) == key(b) != ''


@pytest.mark.parametrize('a, b', [
    ('Dina: Drugi deo', 'Dina: Treći deo'),
    ('Dina: Drugi deo Re-release', 'Dina Re-release'),      # вторая часть и первая — разные фильмы
    ('Avatar', 'Avatar 2'),
    ('Veliko putovanje 3: Trka oko sveta', 'Veliko putovanje 2'),
    ('Toska - UŽIVO', 'TOSKA, NY MET, 2027'),               # разные трансляции не склеиваем правилами
    ('Labudovo jezero - UŽIVO', 'Krcko Oraščić'),
])
def test_different_films_keep_different_keys(a, b):
    assert key(a) != key(b)


def test_empty_and_only_tail_do_not_crash():
    assert canonical_key('') == ''
    assert canonical_key(None) == ''
    assert canonical_key('re release') == 're release'        # ключ не должен стать пустым


def test_numbers_over_ten_untouched():
    assert key('Pas 51') == 'pas 51'


def test_movie_key_prefers_serbian_title_and_falls_back_to_original():
    ev = CinemaEvent(cinema_name='c', cinema_maps_url='', cinema_url='', movie_title='Dune: Part Three',
                     showtime=datetime(2026, 12, 1, 20), ticket_url='u', source='s',
                     movie_title_serbian='Dina: Treći Deo')
    assert movie_key_of(ev) == 'dina treći deo'
    ev.movie_title_serbian = ''
    assert movie_key_of(ev) == 'dune part three'
