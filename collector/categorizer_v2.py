# collector/categorizer_v2.py
# Общий категоризатор v2 — единый словарь для всех каналов.
#
# ВОЗВРАТ: list[str] — список категорий события.
#   - Первый элемент ([0]) — ГЛАВНАЯ категория (пишется в messages.event_type).
#   - Весь список — все подходящие категории (пишется в messages.event_types).
#   - Пустой список [] — событие ни к чему не отнесено (аналог "Другое").
#
# Отличия от предыдущей версии:
# - categorize() возвращает list[str] вместо str (мультикатегории).
# - Логика primary/secondary внутри _classify() пока НЕ активирована:
#   используется текущая одно-категорийная логика (score + тай-брейк),
#   результат оборачивается в список. Когда словарь разметим на
#   primary/secondary — заменяется только тело _classify().
#
# Активная формула: Вариант Б (сумма весов совпадений)
# Переключение между v1 и v2: см. CATEGORIZER_SWITCH.md

from __future__ import annotations

from pathlib import Path

import yaml
from loguru import logger


_CONFIG: dict | None = None
_CONFIG_PATH = Path(__file__).parent.parent / 'categorizers_v2.yaml'

DEFAULT_CATEGORY = 'Другое'

# Каналы с кастомным парсером: если событие получено через
# специфичную логику парсера (position != 0) — категория присваивается
# напрямую, без анализа текста.
CUSTOM_PARSER_CATEGORIES: dict[str, str] = {
    '@zarko_tusic': 'Концерты',
    '@runningbeg': 'Спорт/активный отдых',
}

# Канальные переопределения по ключевым словам.
CHANNEL_KEYWORD_OVERRIDES: dict[str, tuple[list[str], str]] = {
    '@kuralesya_pro': (
        ['краоке', 'краоке', 'рок-краоке', 'рок краоке', 'karaoke', "караоке", "рок караоке", "рок-караоке"],
        'Игры и развлечения',
    ),
}


def _load_config() -> dict:
    global _CONFIG
    if _CONFIG is None:
        with open(_CONFIG_PATH, 'r', encoding='utf-8') as f:
            _CONFIG = yaml.safe_load(f)
    return _CONFIG


def _score_category(text_lower: str, keywords) -> int:
    """
    Взвешенный score категории.
    keywords — dict[str,int] (слово→вес) либо list[str] (вес=1).
    """
    if isinstance(keywords, dict):
        return sum(w for kw, w in keywords.items() if kw.lower() in text_lower)
    return sum(1 for kw in keywords if kw.lower() in text_lower)


def _classify(text: str, categories: dict) -> list[str]:
    """
    Классификация по SECONDARY-словарю (вызывается, когда primary пусто).
    Возвращает ВСЕ категории с максимальным score. При равенстве — все они
    (ранга больше нет). Пустой список = ни одной категории.
    """
    text_lower = text.lower()
    scores: list[tuple[int, str]] = []
    for category, keywords in categories.items():
        if not keywords:
            continue
        found = _score_category(text_lower, keywords)
        if found:
            scores.append((found, category))

    if not scores:
        return []

    top = max(s for s, _ in scores)
    return [c for s, c in scores if s == top]


def categorize(text: str, channel: str, position: int = 0) -> list[str]:
    """
    Определяет категории события.

    Возврат: list[str]
      [0]  — главная категория
      весь — все подходящие
      []   — ни к чему не отнесено (в БД: event_type=NULL/'Другое', event_types={})

    Порядок:
      0. custom parser (position != 0) → [фиксированная]
      1. fixed_channels → [фиксированная]
      2. channel keyword override → [переопределённая]
      3. общий словарь → _classify()
    """
    config = _load_config()
    channel_normalized = channel if channel.startswith('@') else f'@{channel}'

    # Шаг 0 — кастомный парсер
    if position != 0 and channel_normalized in CUSTOM_PARSER_CATEGORIES:
        forced = CUSTOM_PARSER_CATEGORIES[channel_normalized]
        logger.info(
            f"CATEGORIZE_V2 | channel={channel} | "
            f"custom_parser (position={position}) → [{forced!r}]"
        )
        return [forced]

    # Шаг 1 — fixed_channels
    fixed = config.get('fixed_channels') or {}
    if channel_normalized in fixed:
        forced = fixed[channel_normalized]
        logger.debug(f"CATEGORIZE_V2 | channel={channel} | fixed → [{forced!r}]")
        return [forced]

    # Шаг 2 — канальные переопределения
    if channel_normalized in CHANNEL_KEYWORD_OVERRIDES:
        kws, cat = CHANNEL_KEYWORD_OVERRIDES[channel_normalized]
        low = text.lower()
        matched = next((kw for kw in kws if kw.lower() in low), None)
        if matched:
            logger.info(
                f"CATEGORIZE_V2 | channel={channel} | "
                f"keyword_override={matched!r} → [{cat!r}]"
            )
            return [cat]

    # Шаг 3 — общий словарь
    categories = config.get('categories', {})
    result = _classify_multi(text, categories)

    logger.debug(
        f"CATEGORIZE_V2 | channel={channel} | "
        f"{'result=' + str(result) if result else 'no match → []'}"
    )
    return result


# ─────────────────────────────────────────────────────────────
# ЗАГОТОВКА под primary/secondary (НЕ активна).
# Активируется, когда categorizers_v2.yaml разметим на два уровня:
#
#   categories:
#     "Спорт/активный отдых":
#       primary:   {"тренировк": 1, "марафон": 1, ...}
#       secondary: {"pace ": 1, "km ": 1, ...}
#
# Логика:
#   1. primary-совпадения → все категории с хотя бы одним primary
#   2. нет primary → secondary по score (текущая _classify)
#   3. нет ничего → []
# ─────────────────────────────────────────────────────────────
def _classify_multi(text: str, categories: dict) -> list[str]:
    text_lower = text.lower()

    primary_cats: list[str] = []
    secondary_flat: dict[str, dict] = {}

    for category, spec in categories.items():
        if isinstance(spec, dict) and ('primary' in spec or 'secondary' in spec):
            prim = spec.get('primary') or {}
            sec = spec.get('secondary') or {}
        else:
            prim, sec = spec, {}

        if _score_category(text_lower, prim):
            primary_cats.append(category)
        secondary_flat[category] = sec

    if primary_cats:
        return primary_cats

    return _classify(text, secondary_flat)
