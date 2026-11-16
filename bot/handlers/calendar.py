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


NOOP = 'catnoop'
WRAP_TOASTS = {'wp': '🔁 К самому дальнему месяцу', 'wn': '🔁 К ближайшему месяцу'}


def month_carousel(year: int, month: int, dates, max_date: date) -> dict:
    """Соседние месяцы по кругу, только месяцы, где есть дни с событиями (от сегодня до max_date).

    Возвращает {'prev': ((год, месяц), замыкание?), 'next': (...)}; None — других месяцев нет (стрелка с подсказкой).
    «Назад» из самого раннего месяца открывает самый дальний, «вперёд» из самого дальнего — самый ранний.
    """
    today = date.today()
    months = sorted({(d.year, d.month) for d in dates if today <= d <= max_date})
    cur = (year, month)
    if not months:
        months = [(today.year, today.month)]
    before = [m for m in months if m < cur]
    after = [m for m in months if m > cur]
    prev = (before[-1], False) if before else (months[-1], True)
    nxt = (after[0], False) if after else (months[0], True)
    return {
        'prev': None if prev[0] == cur else prev,
        'next': None if nxt[0] == cur else nxt,
    }


def _nav_data(prefix: str, step, direction: str) -> str:
    if step is None:
        return f'{NOOP}:{direction}'
    (y, m), wrapped = step
    return f'{prefix}:{y}-{m:02d}' + (f":{'wp' if direction == 'prev' else 'wn'}" if wrapped else '')


def month_header(year: int, month: int, dates, max_date: date, nav_prefix: str, with_arrows: bool = True) -> list:
    """Шапка календаря: «◀️ Месяц год ▶️» (стрелки по кругу) или одно название месяца."""
    title = InlineKeyboardButton(f'{MONTHS_RU[month]} {year}', callback_data=NOOP)
    if not with_arrows:
        return [title]
    car = month_carousel(year, month, dates, max_date)
    return [
        InlineKeyboardButton('◀️', callback_data=_nav_data(nav_prefix, car['prev'], 'prev')),
        title,
        InlineKeyboardButton('▶️', callback_data=_nav_data(nav_prefix, car['next'], 'next')),
    ]


def parse_nav_target(parts: list, year: int, month: int):
    """Нажатие стрелки месяца: parts — то, что после префикса. 'YYYY-MM[:wp|wn]' или прежние 'prev'/'next'.
    Возвращает (год, месяц, подсказка или None); None — не разобрали."""
    if not parts:
        return None
    token = parts[0]
    toast = WRAP_TOASTS.get(parts[1]) if len(parts) > 1 else None
    if token in ('prev', 'next'):
        month += -1 if token == 'prev' else 1
        if month < 1:
            month, year = 12, year - 1
        elif month > 12:
            month, year = 1, year + 1
        return year, month, None
    try:
        y, m = (int(x) for x in token.split('-'))
        date(y, m, 1)
    except ValueError:
        return None
    return y, m, toast


def build_calendar(
    year: int,
    month: int,
    dates_with_events: set,
    max_date: date,
    callback_prefix: str,
    nav_prefix: str,
) -> list:
    """Календарь месяца: шапка «◀️ Месяц год ▶️» (стрелки по кругу), дни недели, недели."""
    today = date.today()
    rows = [
        month_header(year, month, dates_with_events, max_date, nav_prefix),
        [InlineKeyboardButton(d, callback_data=NOOP) for d in ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс']],
    ]
    for week in calendar.monthcalendar(year, month):
        row = []
        for day in week:
            if day == 0:
                row.append(InlineKeyboardButton(' ', callback_data=NOOP))
                continue
            current_date = date(year, month, day)
            if current_date < today or current_date > max_date or current_date not in dates_with_events:
                row.append(InlineKeyboardButton('·', callback_data=NOOP))
            else:
                row.append(InlineKeyboardButton(str(day), callback_data=f'{callback_prefix}:{current_date.isoformat()}'))
        rows.append(row)
    return rows


MONTHS_GEN = [
    '', 'января', 'февраля', 'марта', 'апреля', 'мая', 'июня',
    'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря'
]


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
                              в шапке «◀️ Месяц год ▶️»; на границе стрелка показывает подсказку (catnoop:prev|next).
      start задан, end=None — выбрано начало: активные дни несут начало (catpick:<code>:<ISO>:<начало>), повторное
                              нажатие на то же число открывает ленту этого дня; в шапке только название месяца.
      start и end           — выбран период: дни периода синие (в том числе дни без событий внутри него), нажатие
                              на активный день начинает выбор заново; в шапке только название месяца.
    Всё нужное для обработки лежит в callback_data кнопки, состояние бота не хранится (переживает перезапуск).
    """
    today = date.today()
    lo = start
    hi = end or start
    selecting = start is not None
    rows = [month_header(year, month, active, max_date, f'catcal_nav:{code}', with_arrows=not selecting)]
    rows.append([InlineKeyboardButton(d, callback_data=NOOP) for d in ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс']])

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

    pool = context.bot_data.get('pool')
    year = context.user_data.get('calendar_year', date.today().year)
    month = context.user_data.get('calendar_month', date.today().month)
    target = parse_nav_target(query.data.split(':')[1:], year, month)
    if target is None:
        await query.answer('Неверный запрос', show_alert=True)
        return
    year, month, toast = target
    await query.answer(toast)

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
