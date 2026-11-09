"""Чистые функции перевода описаний: определение русского текста, проверка ответа, сборка description_ru."""
from __future__ import annotations

import re

TICKETS_LINK_TEXT = "ссылка на билеты здесь"
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


def escape_md(text: str) -> str:
    """Идентично боту (bot/handlers/contact.py): экранируем _ * ` [ ] ."""
    return re.sub(r"([_*`\[\]])", r"\\\1", text)


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


def build_description_ru(translated: str, source_url: str, max_chars: int) -> str:
    """Обрезка -> escape текста -> Markdown-ссылка на билеты (бот потом снимает хвост-ссылку и экранирование)."""
    truncated = truncate_word_boundary(translated.strip(), max_chars)
    link = f"[{TICKETS_LINK_TEXT}]({source_url})"
    return f"{escape_md(truncated)}\n\n{link}"


def validate(result: str, original: str) -> None:
    """Минимальная проверка: не пусто и не аномально длинно (модель «заболталась»)."""
    if not result or not result.strip():
        raise BadTranslation("empty result")
    if len(result) > MIN_LEN_FOR_RATIO and len(result) > MAX_LEN_RATIO * max(len(original), 1):
        raise BadTranslation(f"suspicious length {len(result)} vs {len(original)}")
