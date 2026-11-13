from datetime import date, datetime

from telegram import InlineKeyboardMarkup, InlineKeyboardButton


_WEEKDAYS = ('Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс')


def format_date_short(day: date) -> str:
    """'Пт 14.11' — день недели и число.месяц."""
    return f'{_WEEKDAYS[day.weekday()]} {day.day:02d}.{day.month:02d}'


def _as_date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    return value if isinstance(value, date) else None


def get_event_nav_keyboard(
    current_index: int,
    total: int,
    surrogate_id: int | str,
    is_favorite: bool,
    is_preview: bool = False,
    category_code: str | None = None,
    event_date=None,
    is_admin: bool = False,
    under_review: bool = False,
    reviewed: bool = False,
) -> InlineKeyboardMarkup:
    """Кнопки карточки.

    category_code — код категории, из ленты которой открыта карточка: появляется кнопка «К категории».
    event_date    — дата события: над стрелками показывается некликабельная кнопка с датой.
    is_admin      — админу добавляется кнопка пересмотра («На пересмотре» / «Пересмотрено» — неактивные).
    """
    fav_btn = InlineKeyboardButton(
        '❤️ В избранное' if not is_favorite else '❤️ Убрать',
        callback_data=f'fav:{"add" if not is_favorite else "remove"}:{surrogate_id}'
    )

    hide_btn = InlineKeyboardButton(
        '🚫 Скрыть',
        callback_data=f'hide:{surrogate_id}'
    )

    rows = []

    top = []
    if is_preview:
        top.append(InlineKeyboardButton('📋 Все описание', callback_data='nav:expand'))
    day = _as_date(event_date)
    if day is not None:
        top.append(InlineKeyboardButton(f'📅 {format_date_short(day)}', callback_data='nav:date'))
    if top:
        rows.append(top)

    rows.append([
        InlineKeyboardButton('🔙', callback_data='nav:prev'),
        InlineKeyboardButton(f'{current_index + 1} / {total}', callback_data='nav:counter'),
        InlineKeyboardButton('🔜', callback_data='nav:next'),
    ])
    rows.append([fav_btn, hide_btn])

    if is_admin:
        if reviewed:
            review_btn = InlineKeyboardButton('✅ Пересмотрено', callback_data='rv:busy')
        elif under_review:
            review_btn = InlineKeyboardButton('⏳ На пересмотре', callback_data='rv:busy')
        else:
            review_btn = InlineKeyboardButton('🔎 Пересмотр', callback_data=f'rv:open:{surrogate_id}')
        rows.append([review_btn])

    rows.append(
        [
            InlineKeyboardButton('🗂 К категории', callback_data=f'cat:{category_code}'),
            InlineKeyboardButton('🏠 В меню', callback_data='nav:menu'),
        ]
        if category_code else
        [InlineKeyboardButton('🏠 В меню', callback_data='nav:menu')]
    )
    return InlineKeyboardMarkup(rows)
