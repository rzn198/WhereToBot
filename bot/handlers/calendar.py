import calendar
from datetime import date
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes

from db.tables import Tables
from bot.handlers.contact import _edit_contact_window
from bot.handlers.events import EVENTS_CITY

MONTHS_RU = [
    '', 'Январь', 'Февраль', 'Март', 'Апрель', 'Май', 'Июнь',
    'Июль', 'Август', 'Сентябрь', 'Октябрь', 'Ноябрь', 'Декабрь'
]


def build_calendar(
    year: int,
    month: int,
    dates_with_events: set,
    max_date: date,
    callback_prefix: str,
    nav_prefix: str,
) -> list:

    today = date.today()
    cal = calendar.monthcalendar(year, month)

    header = [InlineKeyboardButton(d, callback_data='calendar:ignore')
              for d in ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс']]

    rows = [header]

    for week in cal:
        row = []
        for day in week:
            if day == 0:
                row.append(InlineKeyboardButton(' ', callback_data='calendar:ignore'))
                continue

            current_date = date(year, month, day)

            if current_date < today or current_date > max_date:
                row.append(InlineKeyboardButton('·', callback_data='calendar:ignore'))
            elif current_date in dates_with_events:
                date_str = current_date.isoformat()
                row.append(InlineKeyboardButton(
                    str(day),
                    callback_data=f'{callback_prefix}:{date_str}'
                ))
            else:
                row.append(InlineKeyboardButton('·', callback_data='calendar:ignore'))

        rows.append(row)

    current_month = date.today()
    prev_disabled = (year == current_month.year and month == current_month.month)
    next_disabled = (year == max_date.year and month == max_date.month)

    nav = [
        InlineKeyboardButton(
            '◀️' if not prev_disabled else ' ',
            callback_data=f'{nav_prefix}:prev' if not prev_disabled else 'calendar:ignore'
        ),
        InlineKeyboardButton(
            '▶️' if not next_disabled else ' ',
            callback_data=f'{nav_prefix}:next' if not next_disabled else 'calendar:ignore'
        ),
    ]
    rows.append(nav)

    return rows


MONTHS_GEN = [
    '', 'января', 'февраля', 'марта', 'апреля', 'мая', 'июня',
    'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря'
]

NOOP = 'catnoop'


def format_day_ru(day: date) -> str:
    """'19 ноября'."""
    return f'{day.day} {MONTHS_GEN[day.month]}'


def format_period_ru(first: date, last: date) -> str:
    """'19 ноября' для одного дня, '19–22 ноября' для периода в одном месяце."""
    if first == last:
        return format_day_ru(first)
    if (first.year, first.month) == (last.year, last.month):
        return f'{first.day}–{last.day} {MONTHS_GEN[last.month]}'
    return f'{format_day_ru(first)} – {format_day_ru(last)}'


def plural_events(n: int) -> str:
    """'1 событие', '2 события', '5 событий'."""
    if n % 10 == 1 and n % 100 != 11:
        word = 'событие'
    elif n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        word = 'события'
    else:
        word = 'событий'
    return f'{n} {word}'


def build_range_calendar(
    year: int,
    month: int,
    active: set,
    max_date: date,
    code: str,
    start: date | None = None,
    end: date | None = None,
) -> list:
    """Календарь категории с выбором одного дня или периода в пределах одного месяца.

    Состояния:
      start=None            — ничего не выбрано; нажатие на активный день: catpick:<code>:<ISO> (выбрать начало);
                              стрелки листают месяцы.
      start задан, end=None — выбрано начало: активные дни несут начало (catpick:<code>:<ISO>:<начало>), повторное
                              нажатие на то же число открывает ленту этого дня; стрелки неактивны.
      start и end           — выбран период: дни периода синие (в том числе дни без событий внутри него), нажатие
                              на активный день начинает выбор заново; стрелки неактивны.
    Всё нужное для обработки лежит в callback_data кнопки, состояние бота не хранится (переживает перезапуск).
    """
    today = date.today()
    lo = start
    hi = end or start
    selecting = start is not None
    rows = [[InlineKeyboardButton(d, callback_data=NOOP) for d in ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс']]]

    for week in calendar.monthcalendar(year, month):
        row = []
        for day in week:
            if day == 0:
                row.append(InlineKeyboardButton(' ', callback_data=NOOP))
                continue
            current = date(year, month, day)
            in_range = selecting and lo <= current <= hi
            is_active = current in active and today <= current <= max_date
            if is_active:
                if selecting and end is None:
                    data = f'catpick:{code}:{current.isoformat()}:{start.isoformat()}'
                else:
                    data = f'catpick:{code}:{current.isoformat()}'
                row.append(InlineKeyboardButton(str(day), callback_data=data, style='primary' if in_range else None))
            elif in_range:
                row.append(InlineKeyboardButton(str(day), callback_data=NOOP, style='primary'))
            else:
                row.append(InlineKeyboardButton('·', callback_data=NOOP))
        rows.append(row)

    prev_disabled = selecting or (year, month) <= (today.year, today.month)
    next_disabled = selecting or (year, month) >= (max_date.year, max_date.month)
    rows.append([
        InlineKeyboardButton(' ' if prev_disabled else '◀️', callback_data=NOOP if prev_disabled else f'catcal_nav:{code}:prev'),
        InlineKeyboardButton(' ' if next_disabled else '▶️', callback_data=NOOP if next_disabled else f'catcal_nav:{code}:next'),
    ])
    return rows


async def handle_calendar(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    await query.answer()

    pool = context.bot_data.get('pool')
    today = date.today()
    context.user_data['calendar_year'] = today.year
    context.user_data['calendar_month'] = today.month

    await _show_calendar(update, context, pool)


async def handle_calendar_nav(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    await query.answer()

    pool = context.bot_data.get('pool')
    parts = query.data.split(':')
    direction = parts[1]

    year = context.user_data.get('calendar_year', date.today().year)
    month = context.user_data.get('calendar_month', date.today().month)

    if direction == 'prev':
        month -= 1
        if month < 1:
            month = 12
            year -= 1
    elif direction == 'next':
        month += 1
        if month > 12:
            month = 1
            year += 1

    context.user_data['calendar_year'] = year
    context.user_data['calendar_month'] = month

    await _show_calendar(update, context, pool)


async def _show_calendar(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    pool,
) -> None:
    chat_id = update.effective_chat.id
    menu_message_id = context.user_data.get('menu_message_id')

    year = context.user_data.get('calendar_year', date.today().year)
    month = context.user_data.get('calendar_month', date.today().month)

    # дни, в которые есть посты каналов или события сайтов (Белград)
    row = await pool.fetchrow(f"""
            SELECT
                array_agg(DISTINCT d) as dates,
                MAX(d) as max_date
            FROM (
                SELECT date AS d
                FROM {Tables.MESSAGES}
                WHERE date IS NOT NULL
                AND date >= CURRENT_DATE
                AND hidden_by_admin = FALSE
                UNION ALL
                SELECT starts_at
                FROM {Tables.EVENTS}
                WHERE starts_at >= CURRENT_DATE
                AND city = $1
                AND hidden_by_admin = FALSE
            ) t
        """, EVENTS_CITY)
    dates_with_events = set(row['dates'] or [])
    max_date = row['max_date'] if row and row['max_date'] else date.today()

    keyboard = build_calendar(
        year=year,
        month=month,
        dates_with_events=dates_with_events,
        max_date=max_date,
        callback_prefix='calendar_date',
        nav_prefix='calendar_nav',
    )
    keyboard.append([InlineKeyboardButton('🏠 В меню', callback_data='nav:menu')])

    await _edit_contact_window(
        context=context,
        chat_id=chat_id,
        menu_message_id=menu_message_id,
        text=f"📅 *Выберите дату:*\n\n{MONTHS_RU[month]} {year}",
        keyboard=InlineKeyboardMarkup(keyboard),
    )
