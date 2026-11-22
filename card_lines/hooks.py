"""Проверки ИИ-хука кодом: хук пишет модель, но верит ему код только при подтверждении цитатой из источника.

Правила (любое нарушение = хук отбрасывается, название и жанр остаются):
  * цитата-основание (evidence) должна дословно (с точностью до регистра, диакритики и знаков препинания) быть в описании записи
    или в найденных фактах из интернета; цитата с «…» проверяется по частям;
  * если основание только в фактах из интернета, формула должна разрешать интернет (Formula.web);
  * числа в хуке (годы, «8 месяцев») должны быть в цитате или в названии;
  * громкие слова («впервые», «легендарный», «единственный»...) требуют подтверждающего слова в цитате (рус/англ/серб);
  * латинские слова ≥4 букв есть в источнике; нет ссылок, тегов и эмодзи; длина ≤ hook_max.
Время старта (time + time_evidence) принимается только если цитата есть в тексте и содержит это время.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import time

from card_lines.norm import fold, norm
from card_lines.profiles import Formula

_URL_OR_TAG = re.compile(r'https?://|www\.|t\.me/|[@#]\w', re.I)
_EMOJI = re.compile('[\U0001F000-\U0001FFFF☀-➿️‍]')
_LATIN_WORD = re.compile(r'[A-Za-zÀ-žĐđ]{4,}')
_NUMBER = re.compile(r'\d+')
_DASH_PREFIX = re.compile(r'^\s*[—–-]\s*')


def foreign_script(text: str) -> str | None:
    """Первый символ не из кириллицы/латиницы/цифр/обычной пунктуации (иероглифы, арабица и т. п. от сбоя модели) или None."""
    for ch in text:
        o = ord(ch)
        if o < 0x250 or 0x400 <= o <= 0x4FF or 0x2000 <= o <= 0x206F or ch in '№€₽…':
            continue
        return ch
    return None


def balance_quotes(t: str) -> str:
    """Убирает одиночные кавычки без пары: «Четыре времени года -> Четыре времени года."""
    while t.count('«') > t.count('»'):
        i = t.rfind('«') if t.endswith('«') else t.find('«')
        t = (t[:i] + t[i + 1:]).strip()
    while t.count('»') > t.count('«'):
        i = t.find('»')
        t = (t[:i] + t[i + 1:]).strip()
    if t.count('"') % 2:
        t = t.replace('"', '', 1) if t.startswith('"') else t[::-1].replace('"', '', 1)[::-1]
    return t.strip()

# корень в хуке (по-русски) -> подтверждающие фрагменты в цитате (русский, английский, сербский)
WATCH: dict[str, tuple[str, ...]] = {
    'впервые': ('впервые', 'first time', 'prvi put', 'prvi nastup', 'debi', 'premijer', 'premiere'),
    'легендарн': ('легендарн', 'legend', 'legendar'),
    'культов': ('культов', 'cult', 'kult'),
    'единственн': ('единственн', 'only', 'jedin', 'sole'),
    'лучш': ('лучш', 'best', 'najbolj', 'najbol'),
    'эксклюзив': ('эксклюзив', 'exclusive', 'ekskluziv'),
    'последн': ('последн', 'last', 'final', 'poslednj', 'zadnj'),
    'уникальн': ('уникальн', 'unique', 'jedinstven'),
}


class BadHook(ValueError):
    """Хук не прошёл проверку (причина в тексте)."""


@dataclass(frozen=True)
class Hook:
    text: str
    formula: int            # номер формулы (с 1)
    evidence: str
    source: str             # 'text' | 'web'


def _segments(evidence: str) -> list[str]:
    parts = re.split(r'…|\.{3}', evidence)
    return [n for n in (norm(p) for p in parts) if len(n) >= 6]


def locate_evidence(evidence: str, text_norm: str, facts_norm: str) -> str | None:
    """'text' — цитата целиком в описании записи; 'web' — в фактах из интернета (или частями в обоих); None — не найдена."""
    segs = _segments(evidence)
    if not segs:
        return None
    if all(s in text_norm for s in segs):
        return 'text'
    if facts_norm and all(s in facts_norm or s in text_norm for s in segs):
        return 'web'
    return None


def check_hook(obj: dict, *, title: str, formulas: tuple[Formula, ...], hook_max: int, text: str, facts: str = '') -> Hook | None:
    """None — модель хука не дала (нет данных); BadHook — дала, но не прошла проверку; Hook — принят.
    text — описание/текст записи вместе с названием и площадкой; facts — найденные факты из интернета ('' если не искали)."""
    raw = obj.get('hook')
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    if not isinstance(raw, str):
        raise BadHook(f"hook не строка: {raw!r}")
    hook = re.sub(r'\s+', ' ', _EMOJI.sub('', raw.replace('\n', ' '))).strip().strip('«»"\'').strip()
    t = str(title or '').strip()
    if t and fold(hook).startswith(fold(t)):                      # хук идёт после «Название — », название повторять не нужно
        hook = _DASH_PREFIX.sub('', hook[len(t):]).strip()
    hook = balance_quotes(_DASH_PREFIX.sub('', hook))
    bad = foreign_script(hook)
    if bad:
        raise BadHook(f"посторонний символ {bad!r}")
    if len(hook) < 4:
        raise BadHook("хук слишком короткий")
    if len(hook) > hook_max:
        raise BadHook(f"хук длиннее {hook_max} ({len(hook)})")
    if _URL_OR_TAG.search(hook):
        raise BadHook("ссылка или тег в хуке")
    try:
        formula = int(obj.get('formula'))
    except (TypeError, ValueError):
        raise BadHook("нет номера формулы") from None
    if not 1 <= formula <= len(formulas):
        raise BadHook(f"формулы {formula} нет")
    evidence = obj.get('evidence')
    if not isinstance(evidence, str) or len(norm(evidence)) < 6:
        raise BadHook("нет цитаты-основания")
    evidence = re.sub(r'\s+', ' ', evidence).strip()[:300]
    source = locate_evidence(evidence, norm(text), norm(facts))
    if source is None:
        raise BadHook("цитаты нет в источнике")
    if source == 'web' and not formulas[formula - 1].web:
        raise BadHook(f"факты из интернета не разрешены для формулы {formula}")
    ev_norm, title_norm = norm(evidence), norm(t)
    for n in _NUMBER.findall(hook):
        if n not in _NUMBER.findall(ev_norm) and n not in _NUMBER.findall(title_norm):
            raise BadHook(f"числа {n} нет в цитате")
    hook_fold, ev_fold = fold(hook), fold(evidence)
    for stem, proofs in WATCH.items():
        if stem in hook_fold and not any(p in ev_fold for p in proofs):
            raise BadHook(f"«{stem}…» без подтверждения в цитате")
    source_fold = fold(f"{text}\n{facts}")
    for w in _LATIN_WORD.findall(hook):
        if fold(w) not in source_fold:
            raise BadHook(f"слова {w!r} нет в источнике")
    return Hook(hook, formula, evidence, source)


def _time_regex(t: time) -> re.Pattern:
    h, m = t.hour, t.minute
    if m == 0:
        return re.compile(rf'(?<!\d)0?{h}\s*(?:[:.]\s*00|h(?![a-z])|ч(?![а-я])|час(?:а|ов)?(?![а-я])|uhr)', re.I)
    return re.compile(rf'(?<!\d)0?{h}\s*[:.hч]\s*{m:02d}(?!\d)', re.I)


def check_time(raw, evidence, text: str) -> time | None:
    """Время старта из текста: формат ЧЧ:ММ, цитата есть в тексте и содержит именно это время. Иначе None (без шума)."""
    if not isinstance(raw, str) or not isinstance(evidence, str):
        return None
    m = re.fullmatch(r'\s*([01]?\d|2[0-3]):([0-5]\d)\s*', raw)
    if not m:
        return None
    t = time(int(m.group(1)), int(m.group(2)))
    seg = re.sub(r'\s+', ' ', evidence).strip()
    if len(seg) < 3 or norm(seg) not in norm(text):
        return None
    if t.minute == 0 and t.hour == 0:                  # «00:00» чаще всего заглушка
        return None
    return t if _time_regex(t).search(seg) else None
