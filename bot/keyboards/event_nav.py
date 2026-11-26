from datetime import date, datetime

from telegram import InlineKeyboardMarkup, InlineKeyboardButton

from bot.keyboards.carousel import carousel_row


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
    calendar_back: str | None = None,
    event_date=None,
    is_admin: bool = False,
    under_review: bool = False,
    reviewed: bool = False,
    list_back: str | None = None,
    cinema_favs: int = 0,
) -> InlineKeyboardMarkup:
    """Кнопки карточки.

    category_code — код категории, из ленты которой открыта карточка.
    calendar_back — callback_data возврата в календарь (лента за день или период из календаря категории).
    event_date    — дата события: над стрелками показывается некликабельная кнопка с датой.
    list_back     — callback_data возврата на страницу списка (карточка открыта из окна категории).
    cinema_favs   — число сеансов киноафиши в запланированном (только в ленте избранного): кнопка «🎬 Кино (N)».

    Возврат одной кнопкой «⬅️ Назад» (рядом с «🏠 В меню»), куда она ведёт — по тому, откуда открыта карточка:
    из окна категории — на ту же страницу списка; из ленты за день или период — в календарь; из ленты всей категории — в категорию;
    в остальных случаях (избранное, пуш, поиск) кнопки нет.
    Стрелки — карусель: на каждой кнопке номер карточки, куда она ведёт («◀️ 8/22» · «10/22 ▶️»); с первой карточки «назад» ведёт на последнюю.
    is_admin      — админу добавляется кнопка пересмотра («На пересмотре» / «Пересмотрено» — неактивные).
    """
    fav_btn = InlineKeyboardButton(
        '❤️ Напомнить' if not is_favorite else '💔 Удалить',
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

    arrows = carousel_row(current_index, total, 'nav:prev', 'nav:next')
    if arrows:
        rows.append(arrows)
    rows.append([fav_btn, hide_btn])
    if cinema_favs:          # в запланированном есть сеансы киноафиши: вход в их список
        rows.append([InlineKeyboardButton(f'🎬 Кино ({cinema_favs})', callback_data='cfl')])

    if is_admin:
        if reviewed:
            review_btn = InlineKeyboardButton('✅ Пересмотрено', callback_data='rv:busy')
        elif under_review:
            review_btn = InlineKeyboardButton('⏳ На пересмотре', callback_data='rv:busy')
        else:
            review_btn = InlineKeyboardButton('🔎 Пересмотр', callback_data=f'rv:open:{surrogate_id}')
        rows.append([review_btn])

    back = list_back or calendar_back or (f'cat:{category_code}' if category_code else None)
    if back:
        rows.append([
            InlineKeyboardButton('⬅️ Назад', callback_data=back),
            InlineKeyboardButton('🏠 В меню', callback_data='nav:menu'),
        ])
    else:
        rows.append([InlineKeyboardButton('🏠 В меню', callback_data='nav:menu')])
    return InlineKeyboardMarkup(rows)
