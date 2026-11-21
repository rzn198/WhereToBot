"""Нормализация текста для проверок: без регистра, диакритики (š→s, đ→d, й→и) и знаков препинания."""
from __future__ import annotations

import re
import unicodedata

_NON_WORD = re.compile(r'[^\w]+', re.U)


def fold(s: str) -> str:
    """Без регистра и диакритики: для сравнения слов названия и хука с источником."""
    s = s.replace('đ', 'd').replace('Đ', 'D')
    s = unicodedata.normalize('NFKD', s)
    return ''.join(ch for ch in s if not unicodedata.combining(ch)).casefold()


def norm(s: str) -> str:
    """fold + все не-буквы/цифры в один пробел: для поиска цитаты в тексте независимо от кавычек, тире и переносов."""
    return _NON_WORD.sub(' ', fold(s or '')).strip()
