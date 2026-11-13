"""Категории ленты: список, порядок, названия кнопок и общие условия выборки.

Лента по категории и счётчики на кнопках используют ОДНИ И ТЕ ЖЕ условия
(MESSAGES_FUTURE / EVENTS_FUTURE), иначе число на кнопке разошлось бы с
реальной длиной ленты.

Параметры SQL (везде одинаковые):
    $1 user_id   $2 город событий   $3 горизонт в днях   $4... массивы названий категорий
"""

from __future__ import annotations

from dataclasses import dataclass

from db.tables import Tables
from shared.config import config

DEFAULT_HORIZON_DAYS = 365


@dataclass(frozen=True)
class Category:
    code: str                       # короткий код для callback_data (лимит Telegram 64 байта)
    emoji: str
    label: str                      # название на кнопке
    db_values: tuple[str, ...]      # какие значения event_types относятся к категории


# Порядок = порядок кнопок в меню.
# «Семейные/детские»: у старых записей может быть прежнее длинное название,
# поэтому в выборку входят оба значения (данные переписывать не нужно).
CATEGORIES: tuple[Category, ...] = (
    Category('concerts', '🎵', 'Концерты', ('Концерты',)),
    Category('theater',  '🎭', 'Кино/Театр', ('Кино/Театр',)),
    Category('expo',     '🖼', 'Выставки/Ярмарки', ('Выставки/Ярмарки',)),
    Category('festival', '🎪', 'Фестивали', ('Фестивали',)),
    Category('learn',    '🎓', 'Мастер-классы/Нетворкинг', ('Мастер-классы/Нетворкинг',)),
    Category('family',   '👪', 'Семейные/детские',
             ('Семейные/детские', 'Семейные/детские мероприятия')),
    Category('fun',      '🎲', 'Игры и развлечения', ('Игры и развлечения',)),
    Category('trips',    '🚌', 'Поездки и экскурсии', ('Поездки и экскурсии',)),
    Category('sport',    '🏃', 'Спорт/активный отдых', ('Спорт/активный отдых',)),
)

_BY_CODE = {c.code: c for c in CATEGORIES}

# Категории, вынесенные кнопками в главное меню (порядок = порядок кнопок). Остальные открываются через «Остальные события».
MAIN_MENU_CODES: tuple[str, ...] = ('concerts', 'fun')


def get_category(code: str) -> Category | None:
    return _BY_CODE.get(code)


def horizon_days() -> int:
    """Как далеко вперёд показываем (граница окна коллектора, config cleanup.events_max_future_days)."""
    cfg = config.get('cleanup', {}) or {}
    try:
        return int(cfg.get('events_max_future_days', DEFAULT_HORIZON_DAYS))
    except (TypeError, ValueError):
        return DEFAULT_HORIZON_DAYS


# Будущие посты каналов: от сегодня до горизонта, не скрытые админом и не скрытые пользователем.
MESSAGES_FUTURE = f"""
    date >= CURRENT_DATE
    AND date <= CURRENT_DATE + make_interval(days => $3)
    AND hidden_by_admin = FALSE
    AND surrogate_id NOT IN (SELECT msg_id FROM {Tables.HIDDEN_EVENTS} WHERE user_id = $1)
"""

# Будущие события сайтов: только выбранный город, не скрытые админом (дубли) и не скрытые пользователем.
EVENTS_FUTURE = f"""
    starts_at >= CURRENT_DATE
    AND starts_at <= CURRENT_DATE + make_interval(days => $3)
    AND city = $2
    AND hidden_by_admin = FALSE
    AND id NOT IN (SELECT event_id FROM {Tables.EVENT_HIDDEN} WHERE user_id = $1)
"""


def rest_categories() -> tuple[Category, ...]:
    """Категории, которые открываются через «Остальные события» (все, кроме вынесенных в главное меню)."""
    return tuple(c for c in CATEGORIES if c.code not in MAIN_MENU_CODES)


def feed_sql(day: bool = False) -> str:
    """Лента категории; $4 — массив названий категории. С day=True ещё и $5 — один день (date)."""
    m_day = "AND date = $5::date" if day else ""
    e_day = "AND starts_at = $5::date" if day else ""
    return f"""
        SELECT kind, id FROM (
            SELECT 0 AS ord, 'm' AS kind, surrogate_id AS id,
                   date AS d, created_at AS c
            FROM {Tables.MESSAGES}
            WHERE {MESSAGES_FUTURE} AND event_types && $4::text[] {m_day}
            UNION ALL
            SELECT 1, 'e', id, starts_at, created_at
            FROM {Tables.EVENTS}
            WHERE {EVENTS_FUTURE} AND event_types && $4::text[] {e_day}
        ) u
        ORDER BY d, ord, c, id
    """


def category_dates_sql() -> str:
    """Дни, в которые у категории есть элементы ленты (для календаря); те же условия, что у feed_sql. $4 — массив названий."""
    return f"""
        SELECT array_agg(DISTINCT d) AS dates, MAX(d) AS max_date FROM (
            SELECT date AS d FROM {Tables.MESSAGES}
            WHERE {MESSAGES_FUTURE} AND event_types && $4::text[]
            UNION ALL
            SELECT starts_at FROM {Tables.EVENTS}
            WHERE {EVENTS_FUTURE} AND event_types && $4::text[]
        ) t
    """


def counts_sql(with_rest: bool = False) -> str:
    """Счётчики всех категорий одним запросом; массивы категорий — $4, $5, ... по порядку CATEGORIES.
    С with_rest=True добавляется колонка rest — число элементов, попавших хотя бы в одну из «остальных» категорий
    (мульти-категория считается один раз); её массив названий — следующий параметр после категорий."""
    cols = [f"count(*) FILTER (WHERE et && ${4 + i}::text[]) AS c{i}" for i in range(len(CATEGORIES))]
    if with_rest:
        cols.append(f"count(*) FILTER (WHERE et && ${4 + len(CATEGORIES)}::text[]) AS rest")
    cols = ',\n            '.join(cols)
    return f"""
        SELECT
            {cols}
        FROM (
            SELECT event_types AS et FROM {Tables.MESSAGES} WHERE {MESSAGES_FUTURE}
            UNION ALL
            SELECT event_types FROM {Tables.EVENTS} WHERE {EVENTS_FUTURE}
        ) t
    """


async def load_menu_counts(pool, user_id: int, city: str) -> dict[str, int]:
    """{код категории: число элементов} и ключ 'rest' — для счётчиков админа в главном меню."""
    rest_values = sorted({v for c in rest_categories() for v in c.db_values})
    row = await pool.fetchrow(
        counts_sql(with_rest=True), user_id, city, horizon_days(),
        *[list(c.db_values) for c in CATEGORIES], rest_values,
    )
    out = {c.code: row[f'c{i}'] for i, c in enumerate(CATEGORIES)}
    out['rest'] = row['rest']
    return out
