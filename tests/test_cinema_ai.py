"""AI киноафиши без БД: разбор ответов модели, защита склейки, подбор похожих названий, жанры, настройки."""
import json

import pytest

from cinema_ai import genres as G
from cinema_ai import titles as T
from cinema_ai.service import needs_titles, needs_translation, plan
from cinema_ai.settings import CinemaAISettings, cinema_ai_settings

ST = CinemaAISettings()


def answer(*films):
    return json.dumps({'films': list(films)}, ensure_ascii=False)


# ---------------------------------------------------------------- названия
def test_parse_titles_ok_and_cleanup():
    out, bad = T.parse_titles(answer({'id': 1, 'en': ' Dune:  Part Three ', 'ru': 'Дюна: Часть третья', 'same_as': None}), {1}, {1})
    assert not bad and out[1].en == 'Dune: Part Three' and out[1].ru == 'Дюна: Часть третья'


def test_parse_titles_accepts_code_fence_and_text_around():
    text = '```json\n' + answer({'id': 1, 'en': 'Hexed', 'ru': None, 'same_as': None}) + '\n```'
    out, bad = T.parse_titles(text, {1}, {1})
    assert out[1].ru is None and not bad


@pytest.mark.parametrize('en', ['', 'Дюна', '   ', 'x' * 200, '???'])
def test_parse_titles_rejects_bad_english(en):
    out, bad = T.parse_titles(answer({'id': 1, 'en': en, 'ru': None, 'same_as': None}), {1}, {1})
    assert 1 in bad and not out


def test_parse_titles_latin_ru_is_dropped_not_failed():
    out, bad = T.parse_titles(answer({'id': 1, 'en': 'Hexed', 'ru': 'Hexed', 'same_as': None}), {1}, {1})
    assert out[1].ru is None and not bad


def test_parse_titles_missing_film_and_foreign_ids():
    out, bad = T.parse_titles(answer({'id': 1, 'en': 'A', 'ru': None, 'same_as': None},
                                     {'id': 99, 'en': 'Z', 'ru': None, 'same_as': None}), {1, 2}, {1, 2})
    assert set(out) == {1} and bad == {2: 'missing in answer'}


def test_parse_titles_same_as_must_be_listed_and_not_self():
    out, _ = T.parse_titles(answer({'id': 1, 'en': 'A', 'ru': None, 'same_as': 1},
                                   {'id': 2, 'en': 'B', 'ru': None, 'same_as': 777},
                                   {'id': 3, 'en': 'C', 'ru': None, 'same_as': 1}), {1, 2, 3}, {1, 2, 3})
    assert out[1].same_as is None and out[2].same_as is None and out[3].same_as == 1


@pytest.mark.parametrize('text', ['', 'не json', '{"films": 5}', '[1,2]'])
def test_parse_titles_garbage_raises(text):
    with pytest.raises(T.BadTitles):
        T.parse_titles(text, {1}, {1})


def test_number_signature_across_languages():
    assert T.number_signature(['Dune: Part Two', 'Dina: Drugi deo']) == frozenset({2})
    assert T.number_signature(['3 nedelje posle', 'Three weeks after']) == frozenset({3})
    assert T.number_signature(['Avatar']) == frozenset()


@pytest.mark.parametrize('a, b, ok', [
    (['Dune: Part Three', 'Dina: Treći Deo'], ['Dune Part 3'], True),
    (['Avatar'], ['Avatar 2'], False),
    (['Dina: Drugi deo'], ['Dina: Treći deo'], False),
    (['Toska, NY MET, 2027'], ['Toska, NY MET, 2026'], False),
    (['Michael', 'Majkl'], ['Majkl / Michael'], True),
])
def test_numbers_compatible(a, b, ok):
    assert T.numbers_compatible(a, b) is ok


def test_find_candidates_orders_by_similarity_and_respects_limit_and_junk():
    variants = {
        1: ['Dune: Part Three', 'Dina: Treći Deo'],
        2: ['Dune Part 3'],
        3: ['Dune: Part Two'],
        4: ['Kuća na mesecu'],
        5: ['RBO 2026 2027', 'Labudovo jezero'],
        6: ['RBO 2026 2027', 'Toska'],
        7: ['RBO 2026 2027', 'Krcko Oraščić'],
    }
    assert T.find_candidates(1, variants, 0.72, 3)[0] in (2, 3)
    assert 4 not in T.find_candidates(1, variants, 0.72, 3)
    assert T.find_candidates(1, variants, 0.72, 0) == []
    junk = T.junk_titles(variants)
    assert junk == {'rbo 2026 2027'}
    assert T.find_candidates(5, variants, 0.72, 3, junk) == []        # общий мусорный «оригинал» не делает фильмы похожими
    assert 6 in T.find_candidates(5, variants, 0.72, 3)               # без защиты они бы склеились по мусору


def test_resolve_merges_chains_cycles_and_self():
    assert T.resolve_merges({1: 2, 2: 3}) == {1: 3, 2: 3}
    assert T.resolve_merges({1: 2, 2: 1}) == {2: 1}
    assert T.resolve_merges({}) == {}


def test_prompt_marks_context_and_trims_description():
    text = T.build_user_prompt([(1, ['A', 'B'], 'x ' * 200)], [(2, ['C'], '')])
    lines = text.split('\n')
    assert lines[0] == 'TASK:' and lines[2] == 'CONTEXT:' and lines[3] == '2 | C'
    assert len(lines[1]) < 200


# ---------------------------------------------------------------- жанры
def test_split_terms_dedupes_and_trims():
    assert G.split_terms('Akcioni , Komedija,  Akcioni;Triler') == ['Akcioni', 'Komedija', 'Triler']
    assert G.split_terms(None) == [] and G.split_terms('') == []


def test_parse_genres_filters_to_allowed_and_keeps_null():
    text = '{"labels": {"Akcija": "Боевик", "Filmovi": null, "SF": "научная фантастика", "Drama": "драма"}}'
    out = G.parse_genres(text, ['Akcija', 'Filmovi', 'SF', 'Drama', 'Horor'])
    assert out == {'Akcija': 'боевик', 'Filmovi': None, 'SF': None, 'Drama': 'драма'}      # 'Horor' нет в ответе — спросим снова


def test_parse_genres_garbage_raises():
    with pytest.raises(ValueError):
        G.parse_genres('nope', ['A'])


def test_film_genres_priority_first_main_and_dedupe():
    gm = {'Akcija': 'боевик', 'Akcijski': 'боевик', 'Komedija': 'комедия', 'Filmovi': None, 'Drama': 'драма'}
    rows = [('roda', 'Komedija, Drama'), ('cineplexx', 'Akcija, Komedija'), ('cinegrand', 'Filmovi, Akcijski')]
    assert G.film_genres(rows, gm, ('cineplexx', 'cinestar', 'cinegrand', 'mts_dvorana', 'tuckwood', 'roda')) == ['боевик', 'комедия', 'драма']
    assert G.film_genres([('x', 'Filmovi')], gm, ()) == []


# ---------------------------------------------------------------- план и настройки
def movie(**kw):
    base = dict(id=1, ai_version=None, ai_titles_fails=0, ai_desc_fails=0, description_src_len=500, description_ru=None, translated_src_len=None,
                sessions=1, description_src='x', title_serbian='t')
    base.update(kw)
    return base


def test_needs_titles_by_version_and_fails():
    assert needs_titles(movie(), ST)
    assert not needs_titles(movie(ai_version=T.TITLES_VERSION), ST)
    assert needs_titles(movie(ai_version='old'), ST)
    assert not needs_titles(movie(ai_titles_fails=3), ST)
    assert needs_titles(movie(ai_desc_fails=3), ST)           # неудачи перевода не мешают названиям


def test_needs_translation_thresholds():
    assert needs_translation(movie(), ST)
    assert not needs_translation(movie(description_src_len=0), ST)
    assert not needs_translation(movie(description_ru='р', translated_src_len=500, description_src_len=700), ST)      # 1.4x
    assert needs_translation(movie(description_ru='р', translated_src_len=500, description_src_len=760), ST)          # 1.52x
    assert not needs_translation(movie(description_ru='р', translated_src_len=None), ST)                              # русский оригинал
    assert not needs_translation(movie(ai_desc_fails=3), ST)
    assert needs_translation(movie(ai_titles_fails=3), ST)


def test_plan_caps_each_stage():
    ms = [movie(id=i) for i in range(1, 11)]
    a, b = plan(ms, CinemaAISettings(max_films_per_run=4))
    assert len(a) == 4 and len(b) == 4 and a[0]['id'] == 1


def test_settings_defaults_and_clamping():
    st = cinema_ai_settings({})
    assert st.method == 'off' and not st.enabled and st.max_films_per_run == 40 and st.retranslate_ratio == 1.5
    assert cinema_ai_settings({'method': 'ON'}).enabled
    assert cinema_ai_settings({'method': 'bogus'}).method == 'off'
    assert cinema_ai_settings({'retranslate_ratio': 0.2, 'max_films_per_run': 0}).retranslate_ratio == 1.0
