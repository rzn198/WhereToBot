"""Жанры киноафиши: сербские метки кинотеатров -> русские жанры из фиксированного списка.

Модель переводит УНИКАЛЬНЫЕ метки один раз (в данных их ~30 на все фильмы), результат хранится в cinema_genre_map.
Метка, не являющаяся жанром («Filmovi», «32. FAF»), получает NULL и на карточке не показывается.
"""
from __future__ import annotations

import json
import re

ALLOWED = (
    'боевик', 'комедия', 'драма', 'триллер', 'криминал', 'детектив', 'ужасы', 'фантастика', 'фэнтези',
    'приключения', 'мультфильм', 'семейный', 'документальный', 'биография', 'мелодрама', 'мюзикл',
    'музыка', 'концерт', 'опера', 'балет', 'военный', 'исторический', 'спорт', 'мистика',
)

SYSTEM_PROMPT = (
    "Map cinema genre labels (Serbian or English) to Russian genres. "
    f"Allowed Russian values: {', '.join(ALLOWED)}. "
    "Use null when the label is not a genre (festival or section name, 'Filmovi') or nothing fits. "
    'Answer with JSON only: {"labels":{"Akcija":"боевик","Filmovi":null}}'
)

_SPLIT = re.compile(r'[,;]+')            # «Romantični/ljubavni» — одна метка, по «/» не режем


def split_terms(raw: str | None) -> list[str]:
    """«Akcioni , Komedija» -> ['Akcioni', 'Komedija'] (без пустых и повторов, порядок сохраняется)."""
    out: list[str] = []
    for part in _SPLIT.split(raw or ''):
        t = ' '.join(part.split())
        if t and t not in out:
            out.append(t)
    return out


def build_user_prompt(terms: list[str]) -> str:
    return json.dumps(terms, ensure_ascii=False)


def parse_genres(text: str, requested: list[str]) -> dict[str, str | None]:
    """Разбор ответа: метка -> русский жанр | None. Значения вне списка ALLOWED становятся None (метка не потеряется)."""
    t = re.sub(r'^```(?:json)?\s*|\s*```$', '', text.strip(), flags=re.S).strip()
    start, end = t.find('{'), t.rfind('}')
    if start < 0 or end <= start:
        raise ValueError('no json object')
    data = json.loads(t[start:end + 1])
    labels = data.get('labels') if isinstance(data, dict) else None
    if not isinstance(labels, dict):
        raise ValueError('no labels')
    by_fold = {k.casefold(): v for k, v in labels.items() if isinstance(k, str)}
    out: dict[str, str | None] = {}
    for term in requested:
        if term.casefold() not in by_fold:
            continue                       # метки нет в ответе — не записываем, спросим в следующий раз
        v = by_fold[term.casefold()]
        v = ' '.join(v.split()).lower() if isinstance(v, str) else None
        out[term] = v if v in ALLOWED else None
    return out


def film_genres(terms_by_source: list[tuple[str, str | None]], genre_map: dict[str, str | None],
                priority: tuple[str, ...]) -> list[str]:
    """Русские жанры фильма: сначала источники по приоритету, внутри источника — порядок кинотеатра; без повторов.
    Первый жанр — главный (в списке показывается только он)."""
    rank = {s: i for i, s in enumerate(priority)}
    ordered = sorted(terms_by_source, key=lambda x: rank.get(x[0], len(priority)))
    out: list[str] = []
    for _source, raw in ordered:
        for term in split_terms(raw):
            g = genre_map.get(term)
            if g and g not in out:
                out.append(g)
    return out
