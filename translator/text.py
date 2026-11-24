"""Чистые функции перевода описаний: определение русского текста, проверка ответа, подготовка description_ru."""
from __future__ import annotations

from difflib import SequenceMatcher
import re

ELLIPSIS = "..."

MAX_LEN_RATIO = 3
# ratio проверяем только для достаточно длинных результатов — на очень коротких (1-2 слова)
# даже нормальный перевод может дать высокий ratio
MIN_LEN_FOR_RATIO = 100

SYSTEM_PROMPT = (
    "Ты профессиональный переводчик. Переводишь текст на русский язык. "
    "Выводи ТОЛЬКО перевод — без пояснений, без префиксов, без кавычек. "
    "Если текст уже на русском — верни его без изменений. Сохраняй абзацы."
)

# Буквы, которых нет в русском: сербская кириллица.
_SERB_CYR = re.compile(r"[јљњћђџЈЉЊЋЂЏ]")
_CYR = re.compile(r"[а-яёА-ЯЁ]")
_LAT = re.compile(r"[A-Za-zČčĆćŽžŠšĐđ]")


class BadTranslation(Exception):
    """Ответ модели не прошёл проверку."""


def is_russian(text: str, min_share: float = 0.6) -> bool:
    """Текст уже на русском: кириллицы не меньше min_share среди букв и нет сербских букв (ј љ њ ћ ђ џ).
    Такие описания в модель не отправляем — к ним только добавляется ссылка."""
    t = text or ""
    if _SERB_CYR.search(t):
        return False
    cyr, lat = len(_CYR.findall(t)), len(_LAT.findall(t))
    letters = cyr + lat
    return letters > 0 and cyr / letters >= min_share


def truncate_word_boundary(text: str, limit: int) -> str:
    """Обрезка до limit по границе слова + '...'. Если короче — как есть."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    sp = cut.rfind(" ")
    if sp > 0:
        cut = cut[:sp]
    return cut.rstrip() + ELLIPSIS


def cut_input(text: str, limit: int) -> str:
    """Начало оригинала для модели: до limit символов по границе слова, без многоточия."""
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    sp = cut.rfind(" ")
    return (cut[:sp] if sp > 0 else cut).rstrip()


def build_description_ru(translated: str, max_chars: int) -> str:
    """description_ru = чистый перевод, обрезанный по границе слова (с «...»). Без ссылки и без Markdown-экранирования:
    ссылку на билеты бот строит сам из source_url."""
    return truncate_word_boundary(translated.strip(), max_chars)


MIN_CHECK_LETTERS = 40        # короче — проверки «русский / не копия» не применяем (имена, названия туров)
MIN_CYR_SHARE = 0.3           # ниже — ответ не русский (у верных переводов по замеру не ниже 0.55: много латинских имён)
MAX_SIMILARITY = 0.9          # выше — модель вернула оригинал (у верных переводов по замеру не выше 0.57, у возврата оригинала 0.999)
SIM_WINDOW = 600


def _cyr_share(text: str) -> float:
    cyr, lat = len(_CYR.findall(text)), len(_LAT.findall(text))
    return cyr / (cyr + lat) if cyr + lat else 0.0


def validate(result: str, original: str) -> None:
    """Проверка ответа модели: не пусто; не аномально длинно (модель «заболталась»); это русский текст, а не возвращённый
    оригинал. Последние две проверки — только для оригиналов от MIN_CHECK_LETTERS букв."""
    if not result or not result.strip():
        raise BadTranslation("empty result")
    if len(result) > MIN_LEN_FOR_RATIO and len(result) > MAX_LEN_RATIO * max(len(original), 1):
        raise BadTranslation(f"suspicious length {len(result)} vs {len(original)}")
    if len(_CYR.findall(original)) + len(_LAT.findall(original)) < MIN_CHECK_LETTERS:
        return
    share = _cyr_share(result)
    if share < MIN_CYR_SHARE:
        raise BadTranslation(f"not russian: cyrillic share {share:.2f}")
    a, b = original.strip()[:SIM_WINDOW], result.strip()[:SIM_WINDOW]
    if SequenceMatcher(None, a, b, autojunk=False).ratio() > MAX_SIMILARITY:
        raise BadTranslation("result is the original text")
