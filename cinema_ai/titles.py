"""Этап A: единое название фильма (английское и русское) и склейка вариантов одного фильма.

Чистые функции без БД: подбор похожих названий, защита от склейки частей и сиквелов, промпт и разбор ответа модели.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from cinema_collector.saver import normalize_title

TITLES_VERSION = 'v1'
MAX_TITLE_LEN = 120
DESC_HINT_CHARS = 120

SYSTEM_PROMPT = (
    "You normalize movie titles for a cinema listing in Belgrade. Cinemas give titles in Serbian, English or other "
    "languages, and the 'original' title is sometimes junk (e.g. 'RBO 2026 2027').\n"
    "Input lines: id | title variants separated by ' | ' | start of the description. "
    "Section TASK lists films to answer; section CONTEXT lists known films that may be duplicates (do NOT answer them).\n"
    "For every TASK film return:\n"
    "- en: the international English title as released; for a non-English film the English title if one exists, "
    "otherwise the original-language title in Latin script; for a Serbian-only film the Serbian title. "
    "No format tags (3D, IMAX, dubbed, re-release, live).\n"
    "- ru: the Russian title ONLY if the film has an established Russian release title; otherwise null. "
    "Never translate literally and never invent a title.\n"
    "- same_as: the id of ANOTHER listed film (TASK or CONTEXT) that is the very same film under a different title "
    "variant, otherwise null. Different parts, sequels, seasons, productions, operas or broadcasts are NOT the same film.\n"
    'Answer with JSON only: {"films":[{"id":1,"en":"...","ru":null,"same_as":null}]}'
)

_CYR = re.compile(r'[а-яёА-ЯЁ]')
_LAT = re.compile(r'[A-Za-z]')
_WORD = re.compile(r'[^\W_]+', re.UNICODE)

# Слова-номера (сербский, английский, римские): «Dina: Drugi deo» == «Dune: Part Two» == 2.
_NUMBER_WORDS = {
    'jedan': 1, 'one': 1, 'prvi': 1, 'i': 1,
    'dva': 2, 'dve': 2, 'two': 2, 'drugi': 2, 'ii': 2, 'second': 2,
    'tri': 3, 'three': 3, 'treci': 3, 'treći': 3, 'iii': 3, 'third': 3,
    'cetiri': 4, 'četiri': 4, 'four': 4, 'cetvrti': 4, 'četvrti': 4, 'iv': 4,
    'pet': 5, 'five': 5, 'peti': 5, 'v': 5,
}
_FILLER_ONE = {'i'}      # «i» — сербский союз «и»: как единицу не считаем


def number_signature(titles: list[str]) -> frozenset[int]:
    """Номера частей и годы из названий (по всем вариантам): {2} для «Dune: Part Two» + «Dina: Drugi deo»."""
    nums: set[int] = set()
    for t in titles:
        for w in _WORD.findall(normalize_title(t) or ''):
            if w.isdigit():
                nums.add(int(w))
            elif w in _NUMBER_WORDS and w not in _FILLER_ONE:
                nums.add(_NUMBER_WORDS[w])
    return frozenset(nums)


def numbers_compatible(a: list[str], b: list[str]) -> bool:
    """Склеивать можно только фильмы с одинаковым набором номеров: «Avatar» и «Avatar 2» — разные."""
    return number_signature(a) == number_signature(b)


def _norm_variants(titles: list[str]) -> list[str]:
    return [n for n in (normalize_title(t) for t in titles) if n]


def similarity(a: list[str], b: list[str]) -> float:
    """Максимальная похожесть между вариантами названий двух фильмов."""
    best = 0.0
    for x in _norm_variants(a):
        for y in _norm_variants(b):
            if x == y:
                return 1.0
            best = max(best, SequenceMatcher(None, x, y, autojunk=False).ratio())
    return best


def find_candidates(film_id: int, variants: dict[int, list[str]], threshold: float, limit: int,
                    junk: set[str] | None = None) -> list[int]:
    """id других фильмов с похожими названиями (по убыванию похожести), не больше limit."""
    if limit <= 0:
        return []
    mine = variants[film_id]
    if junk:
        mine = [t for t in mine if normalize_title(t) not in junk]
    scored = []
    for other, titles in variants.items():
        if other == film_id:
            continue
        if junk:
            titles = [t for t in titles if normalize_title(t) not in junk]
        s = similarity(mine, titles)
        if s >= threshold:
            scored.append((s, other))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [i for _, i in scored[:limit]]


def junk_titles(variants: dict[int, list[str]], min_films: int = 3) -> set[str]:
    """Названия, которые у нескольких разных фильмов совпадают (мусор вида «RBO 2026 2027») — по ним не сравниваем."""
    seen: dict[str, set[int]] = {}
    for fid, titles in variants.items():
        for n in _norm_variants(titles):
            seen.setdefault(n, set()).add(fid)
    return {n for n, ids in seen.items() if len(ids) >= min_films}


@dataclass
class TitleResult:
    id: int
    en: str
    ru: str | None
    same_as: int | None


class BadTitles(Exception):
    pass


def film_line(film_id: int, titles: list[str], description: str) -> str:
    hint = ' '.join((description or '').split())[:DESC_HINT_CHARS]
    return f"{film_id} | {' | '.join(titles)} | {hint}".rstrip(' |')


def build_user_prompt(task: list[tuple[int, list[str], str]], context: list[tuple[int, list[str], str]]) -> str:
    parts = ['TASK:']
    parts += [film_line(*row) for row in task]
    if context:
        parts.append('CONTEXT:')
        parts += [film_line(*row) for row in context]
    return '\n'.join(parts)


def _extract_json(text: str):
    t = text.strip()
    t = re.sub(r'^```(?:json)?\s*|\s*```$', '', t, flags=re.S).strip()
    start, end = t.find('{'), t.rfind('}')
    if start < 0 or end <= start:
        raise BadTitles('no json object')
    try:
        return json.loads(t[start:end + 1])
    except ValueError as e:
        raise BadTitles(f'bad json: {e}')


def _clean(value) -> str:
    return ' '.join(str(value).split()) if isinstance(value, (str, int, float)) else ''


def parse_titles(text: str, task_ids: set[int], allowed_ids: set[int]) -> tuple[dict[int, TitleResult], dict[int, str]]:
    """Разбор ответа: (прошедшие проверку, id -> причина отказа). Чужие id в ответе игнорируются."""
    data = _extract_json(text)
    films = data.get('films') if isinstance(data, dict) else None
    if not isinstance(films, list):
        raise BadTitles('no films list')
    ok: dict[int, TitleResult] = {}
    bad: dict[int, str] = {}
    for item in films:
        if not isinstance(item, dict):
            continue
        try:
            fid = int(item.get('id'))
        except (TypeError, ValueError):
            continue
        if fid not in task_ids or fid in ok:
            continue
        en = _clean(item.get('en'))
        ru = _clean(item.get('ru')) or None
        if not en or len(en) > MAX_TITLE_LEN or _CYR.search(en) or not (_LAT.search(en) or any(c.isdigit() for c in en)):
            bad[fid] = f'en: {en[:40]!r}'
            continue
        if ru is not None:
            letters = len(_CYR.findall(ru)) + len(_LAT.findall(ru))
            if len(ru) > MAX_TITLE_LEN or not letters or len(_CYR.findall(ru)) / letters < 0.5:
                ru = None            # русское название только настоящее: не кириллица — отбрасываем, это не провал
        same = item.get('same_as')
        try:
            same = int(same) if same is not None else None
        except (TypeError, ValueError):
            same = None
        if same is not None and (same == fid or same not in allowed_ids):
            same = None
        ok[fid] = TitleResult(fid, en, ru, same)
    for fid in task_ids - set(ok) - set(bad):
        bad[fid] = 'missing in answer'
    return ok, bad


def resolve_merges(pointers: dict[int, int]) -> dict[int, int]:
    """loser -> winner по цепочкам same_as. Цикл (A->B, B->A) склеивается в минимальный id. Возвращает только проигравших."""
    out: dict[int, int] = {}
    for start in pointers:
        seen = [start]
        cur = start
        while cur in pointers:
            nxt = pointers[cur]
            if nxt in seen:
                cur = min(seen[seen.index(nxt):])
                break
            seen.append(nxt)
            cur = nxt
        if cur != start:
            out[start] = cur
    return out
