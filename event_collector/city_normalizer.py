# event_collector/city_normalizer.py
# Единая нормализация города перед сохранением в БД.
# Канон — английское написание. Районы схлопываются в город.
# Чистит HTML-entities, лишние пробели; пустое -> None.

from __future__ import annotations

import re
import html

# Канон -> список алиасов (в нижнем регистре, после чистки).
# Всё, что совпало с алиасом (или самим каноном), приводится к канону.
_CITY_ALIASES = {
    "Belgrade": [
        "belgrade", "beograd", "белград",
        "beograd (zemun)", "zemun", "земун",
        "novi beograd", "нови београд", "new belgrade",
    ],
    "Novi Sad": [
        "novi sad", "нови сад", "čenej", "cenej", "ченеј",
    ],
    "Niš": [
        "niš", "nis", "ниш",
    ],
    "Subotica": ["subotica", "суботица"],
    "Kragujevac": ["kragujevac", "крагујевац"],
    "Kraljevo": ["kraljevo", "краљево"],
    "Kruševac": ["kruševac", "krusevac", "крушевац"],
    "Kladovo": ["kladovo", "кладово"],
    "Leskovac": ["leskovac", "лесковац"],
    "Kovačica": ["kovačica", "kovacica", "ковачица"],
    "Bački Petrovac": ["bački petrovac", "backi petrovac", "бачки петровац"],
    "Čačak": ["čačak", "cacak", "чачак"],
    "Valjevo": ["valjevo", "ваљево"],
    "Vršac": ["vršac", "vrsac", "вршац"],
    "Zrenjanin": ["zrenjanin", "зрењанин"],
    "Sombor": ["sombor", "сомбор"],
    "Pančevo": ["pančevo", "pancevo", "панчево"],
    "Smederevo": ["smederevo", "смедерево"],
    "Sremska Mitrovica": ["sremska mitrovica", "сремска митровица"],
    "Sremski Karlovci": ["sremski karlovci", "сремски карловци"],
    "Ruma": ["ruma", "рума"],
    "Vrdnik": ["vrdnik", "врдник"],
    "Vrnjačka Banja": ["vrnjačka banja", "vrnjacka banja", "врњачка бања"],
    "Palić": ["palić", "palic", "палић"],
    "Krnjevo": ["krnjevo", "крњево"],
    "Budva": ["budva", "будва"],
    "Podgorica": ["podgorica", "подгорица"],
    "Skopje": ["skopje", "скопје", "скопје - карпош", "skopje - karpoš"],
}

# Разворачиваем в плоскую карту alias -> канон (один раз при импорте).
_ALIAS_TO_CANON: dict[str, str] = {}
for _canon, _aliases in _CITY_ALIASES.items():
    _ALIAS_TO_CANON[_canon.lower()] = _canon
    for _a in _aliases:
        _ALIAS_TO_CANON[_a] = _canon


def _clean_raw(value: str) -> str:
    """HTML-entities (&nbsp;), теги, схлопывание пробелов, trim."""
    if not value:
        return ""
    v = html.unescape(value)          # &nbsp; -> \xa0, &amp; -> & и т.п.
    v = re.sub(r"<[^>]+>", " ", v)     # срезаем случайные теги
    v = v.replace("\xa0", " ")          # неразрывный пробел -> обычный
    v = re.sub(r"\s+", " ", v).strip()  # схлопываем пробелы
    return v


def normalize_city(value: str | None) -> str | None:
    """
    Приводит город к каноническому написанию.
    - чистит HTML/пробелы
    - известные алиасы -> канон (английский)
    - пустое -> None
    - неизвестный город -> очищенное исходное (не теряем данные)
    """
    if not value:
        return None
    cleaned = _clean_raw(value)
    if not cleaned:
        return None
    canon = _ALIAS_TO_CANON.get(cleaned.lower())
    if canon:
        return canon
    # неизвестный город — возвращаем очищенным (сохраняем как есть)
    return cleaned


def find_city_in_text(text: str | None, default: str = "Belgrade") -> str:
    """
    Ищет известный город ВНУТРИ произвольной строки (напр. 'Klub X, Niš'
    или 'Foxtrot Cafe Novi Sad'). Возвращает канон найденного города,
    иначе default. Для источников, где город вшит в строку места (biletrs).
    Многословные алиасы проверяются first (novi sad до nis и т.п.).
    """
    if not text:
        return default
    low = _clean_raw(text).lower()
    if not low:
        return default
    # сортируем алиасы по длине убыв. — чтобы 'novi sad' сматчился раньше 'nis',
    # 'bački petrovac' раньше отдельных слов и т.п.
    best_canon = None
    best_len = 0
    for alias, canon in _ALIAS_TO_CANON.items():
        # границы слова, чтобы 'nis' не матчил 'tehnički' и т.п.
        if re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", low):
            if len(alias) > best_len:
                best_canon, best_len = canon, len(alias)
    return best_canon or default
