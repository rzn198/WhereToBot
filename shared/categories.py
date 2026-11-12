"""Единые названия категорий и нормализация старых.

Каноническое название — то, что в `categorizers_v2.yaml`. Прежние названия переименовываются
на ВХОДЕ (сохранение messages/events, ответ AI), чтобы старое имя не попало в БД снова.
Данные, уже лежащие в БД, переписывает миграция 007.
"""
from __future__ import annotations

FAMILY = 'Семейные/детские'

# Служебная метка постоянных музейных экспозиций («запись на каждый день»). НЕ категория ленты: в categorizers_v2.yaml её нет,
# поэтому AI и словарь выдать её не могут. Ставится только правилами в скрейперах (polako, ticketsrs) по точным названиям музеев.
# Бот пока показывает такие события в обзорах по дате; скрытие во всех карточках кроме отдельной карточки «Музеи» — отдельная задача.
MUSEUMS = 'Музеи'

# старое имя -> каноническое
LEGACY_CATEGORY_NAMES: dict[str, str] = {
    'Семейные/детские мероприятия': FAMILY,
}


def normalize_category(name: str) -> str:
    name = (name or '').strip()
    return LEGACY_CATEGORY_NAMES.get(name, name)


def normalize_categories(names) -> list[str]:
    """Переименовать устаревшие названия, убрать пустые и повторы; порядок сохраняется
    (первая категория — главная)."""
    out: list[str] = []
    for n in names or []:
        c = normalize_category(n)
        if c and c not in out:
            out.append(c)
    return out
