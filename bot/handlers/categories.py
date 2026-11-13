"""Категории: «Остальные события» (список), окно категории (сегодня / завтра / на дату) и лента на выбранный день.

Главное меню → кнопка категории (Концерты, Игры и развлечения) или «Остальные события» → список остальных категорий →
окно категории: «Сегодня», «Завтра», «На дату» (календарь только с днями этой категории), «Назад».
Лента на день — filter 'cat:<code>:date:<ISO>'; мульти-категории входят в каждую свою категорию.
"""

from datetime import date, timedelta

from loguru import logger
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from bot.handlers.calendar import MONTHS_RU, build_calendar
from bot.handlers.events import EVENTS_CITY, show_current_event
from bot.handlers.reviews import is_admin_user
from bot.middleware.stats import update_user_stats
from bot.utils.categories import (
    MAIN_MENU_CODES, category_dates_sql, counts_sql, get_category, horizon_days, rest_categories,
)
from bot.utils.messenger import edit_or_send

MENU_TEXT = '🗂 Выберите категорию:'
EMPTY_TEXT = 'Пока нет событий по категориям 🤷'


async def load_category_counts(pool, user_id: int) -> list[tuple]:
    """[(Category, число элементов ленты)] для остальных категорий, где есть что показать."""
    from bot.utils.categories import CATEGORIES
    row = await pool.fetchrow(
        counts_sql(), user_id, EVENTS_CITY, horizon_days(),
        *[list(c.db_values) for c in CATEGORIES],
    )
    rest = {c.code for c in rest_categories()}
    return [(c, row[f'c{i}']) for i, c in enumerate(CATEGORIES) if c.code in rest and row[f'c{i}'] > 0]


def build_menu_keyboard(counts: list[tuple], show_counts: bool = False) -> InlineKeyboardMarkup:
    """Список остальных категорий. Счётчики на кнопках — только админу (show_counts)."""
    rows = [
        [InlineKeyboardButton(f'{c.emoji} {c.label}' + (f' ({n})' if show_counts else ''), callback_data=f'cat:{c.code}')]
        for c, n in counts
    ]
    rows.append([InlineKeyboardButton('⬅️ Назад', callback_data='nav:menu')])
    return InlineKeyboardMarkup(rows)


def build_window_keyboard(code: str) -> InlineKeyboardMarkup:
    """Окно категории: сегодня / завтра в одной строке, «На дату» ниже, «Назад» ниже."""
    back = 'nav:menu' if code in MAIN_MENU_CODES else 'cats:menu'
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton('📅 Сегодня', callback_data=f'catd:{code}:today'),
            InlineKeyboardButton('📅 Завтра', callback_data=f'catd:{code}:tomorrow'),
        ],
        [InlineKeyboardButton('📅 На дату', callback_data=f'catcal:{code}')],
        [InlineKeyboardButton('⬅️ Назад', callback_data=back)],
    ])


async def _render_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, pool) -> None:
    user_id = update.effective_user.id
    counts = await load_category_counts(pool, user_id)
    await edit_or_send(
        context=context,
        chat_id=update.effective_chat.id,
        text=MENU_TEXT if counts else EMPTY_TEXT,
        keyboard=build_menu_keyboard(counts, show_counts=is_admin_user(user_id)),
        parse_mode=None,
    )


async def _render_window(update: Update, context: ContextTypes.DEFAULT_TYPE, code: str) -> None:
    category = get_category(code)
    await edit_or_send(
        context=context,
        chat_id=update.effective_chat.id,
        text=f'{category.emoji} {category.label}\n\nВыберите день:',
        keyboard=build_window_keyboard(code),
        parse_mode=None,
    )


async def handle_categories_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопка «Остальные события» (главное меню) и «Назад» из окна категории."""
    query = update.callback_query
    await query.answer()
    pool = context.bot_data.get('pool')
    logger.info(f"CATEGORIES_MENU | user_id={update.effective_user.id}")
    await _render_menu(update, context, pool)


async def handle_category(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Выбор категории: открываем окно с выбором дня."""
    query = update.callback_query
    pool = context.bot_data.get('pool')
    code = query.data.split(':', 1)[1] if ':' in query.data else ''

    if get_category(code) is None:
        await query.answer('Неизвестная категория', show_alert=True)
        return

    await query.answer()
    await update_user_stats(pool, update)
    logger.info(f"CATEGORY | user_id={update.effective_user.id} | code={code}")
    await _render_window(update, context, code)


async def _open_day(update: Update, context: ContextTypes.DEFAULT_TYPE, code: str, day: date) -> bool:
    """Лента категории на день. False — событий нет (ничего не меняем, окно остаётся)."""
    from bot.handlers.events import load_event_ids

    pool = context.bot_data.get('pool')
    user_id = update.effective_user.id
    filter_name = f'cat:{code}:date:{day.isoformat()}'
    event_ids = await load_event_ids(pool, user_id, filter_name)
    logger.info(f"CATEGORY_DAY | user_id={user_id} | code={code} | day={day} | items={len(event_ids)}")
    if not event_ids:
        return False

    context.user_data['event_ids'] = event_ids
    context.user_data['current_index'] = 0
    context.user_data['current_filter'] = filter_name
    context.user_data['expanded'] = False
    await show_current_event(update, context, pool)
    return True


async def handle_category_day(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """«Сегодня» / «Завтра» в окне категории: catd:<code>:today|tomorrow."""
    query = update.callback_query
    parts = query.data.split(':')
    if len(parts) != 3 or get_category(parts[1]) is None or parts[2] not in ('today', 'tomorrow'):
        await query.answer('Неверный запрос', show_alert=True)
        return
    code, which = parts[1], parts[2]
    day = date.today() + timedelta(days=1 if which == 'tomorrow' else 0)
    if not await _open_day(update, context, code, day):
        await query.answer('На этот день в категории событий нет', show_alert=True)
        return
    await query.answer()


async def handle_category_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """День выбран в календаре категории: catpick:<code>:<ISO>."""
    query = update.callback_query
    parts = query.data.split(':')
    try:
        day = date.fromisoformat(parts[2])
    except (IndexError, ValueError):
        await query.answer('Неверная дата', show_alert=True)
        return
    if len(parts) != 3 or get_category(parts[1]) is None:
        await query.answer('Неверный запрос', show_alert=True)
        return
    if not await _open_day(update, context, parts[1], day):
        await query.answer('На этот день событий нет', show_alert=True)
        return
    await query.answer()


async def _show_category_calendar(update: Update, context: ContextTypes.DEFAULT_TYPE, code: str) -> None:
    pool = context.bot_data.get('pool')
    category = get_category(code)
    year = context.user_data.get('calendar_year', date.today().year)
    month = context.user_data.get('calendar_month', date.today().month)

    row = await pool.fetchrow(
        category_dates_sql(), update.effective_user.id, EVENTS_CITY, horizon_days(), list(category.db_values))
    dates_with_events = set(row['dates'] or [])
    max_date = row['max_date'] if row and row['max_date'] else date.today()

    keyboard = build_calendar(
        year=year, month=month, dates_with_events=dates_with_events, max_date=max_date,
        callback_prefix=f'catpick:{code}', nav_prefix=f'catcal_nav:{code}',
    )
    keyboard.append([InlineKeyboardButton('⬅️ Назад', callback_data=f'cat:{code}')])

    await edit_or_send(
        context=context,
        chat_id=update.effective_chat.id,
        text=f'📅 {category.emoji} {category.label}\n\nВыберите дату:\n{MONTHS_RU[month]} {year}',
        keyboard=InlineKeyboardMarkup(keyboard),
        parse_mode=None,
    )


async def handle_category_calendar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """«На дату» в окне категории: catcal:<code>."""
    query = update.callback_query
    code = query.data.split(':', 1)[1] if ':' in query.data else ''
    if get_category(code) is None:
        await query.answer('Неизвестная категория', show_alert=True)
        return
    await query.answer()
    today = date.today()
    context.user_data['calendar_year'] = today.year
    context.user_data['calendar_month'] = today.month
    await _show_category_calendar(update, context, code)


async def handle_category_calendar_nav(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Листание месяцев: catcal_nav:<code>:prev|next."""
    query = update.callback_query
    parts = query.data.split(':')
    if len(parts) != 3 or get_category(parts[1]) is None or parts[2] not in ('prev', 'next'):
        await query.answer('Неверный запрос', show_alert=True)
        return
    await query.answer()
    year = context.user_data.get('calendar_year', date.today().year)
    month = context.user_data.get('calendar_month', date.today().month)
    month += -1 if parts[2] == 'prev' else 1
    if month < 1:
        month, year = 12, year - 1
    elif month > 12:
        month, year = 1, year + 1
    context.user_data['calendar_year'] = year
    context.user_data['calendar_month'] = month
    await _show_category_calendar(update, context, parts[1])
