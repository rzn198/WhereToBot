import calendar
from datetime import date
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes

from db.tables import Tables
from bot.handlers.contact import _edit_contact_window

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

    row = await pool.fetchrow(f"""
            SELECT
                array_agg(DISTINCT date) as dates,
                MAX(date) as max_date
            FROM {Tables.MESSAGES}
            WHERE date IS NOT NULL
            AND date >= CURRENT_DATE
            AND hidden_by_admin = FALSE
        """)
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
