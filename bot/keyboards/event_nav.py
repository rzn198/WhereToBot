from datetime import date, datetime

from telegram import InlineKeyboardMarkup, InlineKeyboardButton

from bot.formatters.event_card import format_date_ru


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
) -> InlineKeyboardMarkup:
    """Кнопки карточки.

    category_code — код категории, из ленты которой открыта карточка: появляется кнопка «К категории».
    event_date    — дата события: над стрелками показывается некликабельная кнопка с датой.
    is_admin      — админу добавляется кнопка пересмотра (или «На пересмотре», если пересмотр уже открыт).
    """
    counter_or_expand = (
        InlineKeyboardButton('📋 Полностью', callback_data='nav:expand')
        if is_preview else
        InlineKeyboardButton(f'{current_index + 1} / {total}', callback_data='nav:counter')
    )

    fav_btn = InlineKeyboardButton(
        '❤️ В избранное' if not is_favorite else '❤️ Убрать',
        callback_data=f'fav:{"add" if not is_favorite else "remove"}:{surrogate_id}'
    )

    hide_btn = InlineKeyboardButton(
        '🚫 Скрыть',
        callback_data=f'hide:{surrogate_id}'
    )

    rows = []

    day = _as_date(event_date)
    if day is not None:
        rows.append([InlineKeyboardButton(f'📅 {format_date_ru(day)}', callback_data='nav:date')])

    rows.append([
        InlineKeyboardButton('🔙', callback_data='nav:prev'),
        counter_or_expand,
        InlineKeyboardButton('🔜', callback_data='nav:next'),
    ])
    rows.append([fav_btn, hide_btn])

    if is_admin:
        rows.append([
            InlineKeyboardButton('⏳ На пересмотре', callback_data='rv:busy')
            if under_review else
            InlineKeyboardButton('🔎 Пересмотр', callback_data=f'rv:open:{surrogate_id}')
        ])

    rows.append(
        [
            InlineKeyboardButton('🗂 К категории', callback_data=f'cat:{category_code}'),
            InlineKeyboardButton('🏠 В меню', callback_data='nav:menu'),
        ]
        if category_code else
        [InlineKeyboardButton('🏠 В меню', callback_data='nav:menu')]
    )
    return InlineKeyboardMarkup(rows)
