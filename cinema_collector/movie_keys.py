"""Канонический ключ названия фильма: склейка вариантов одного названия без AI.

Работает поверх normalize_title() (нижний регистр, без 3D/IMAX/«sinhronizovano», без знаков).
Дополнительно:
  * убирает хвосты, которые не меняют фильм: «Re-release», «INFINITY VISION», «ScreenX», «4DX»;
  * цифры 1-10 как отдельные слова заменяет сербскими числительными («3 nedelje posle» == «Tri nedelje posle»).
Части и сиквелы остаются разными: «Dina drugi deo» != «Dina treći deo», «avatar» != «avatar 2».
Всё остальное (разный язык, перевод названия) решает AI.
"""
import re

_DIGIT_WORDS = {
    '1': 'jedan', '2': 'dva', '3': 'tri', '4': 'četiri', '5': 'pet',
    '6': 'šest', '7': 'sedam', '8': 'osam', '9': 'devet', '10': 'deset',
}

_TAIL = re.compile(r'\b(re release|infinity vision|screenx|4dx)\b')
_DIGIT = re.compile(r'\b(10|[1-9])\b')
_SPACES = re.compile(r'\s+')


def canonical_key(normalized_title: str) -> str:
    """normalized_title — результат normalize_title(). Пустая строка, если ключ построить нельзя."""
    t = (normalized_title or '').strip()
    if not t:
        return ''
    key = _TAIL.sub(' ', t)
    key = _DIGIT.sub(lambda m: _DIGIT_WORDS[m.group(1)], key)
    key = _SPACES.sub(' ', key).strip()
    return key or t
