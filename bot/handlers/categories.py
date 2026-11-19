"""Категории: «Остальные события» (список), окно категории (сегодня / завтра / на дату) и лента на выбранный день.

Главное меню → кнопка категории (Концерты, Игры и развлечения) или «Остальные события» → список остальных категорий →
окно категории: «Сегодня», «Завтра», «На дату» (календарь только с днями этой категории), «Назад».
Лента на день — filter 'cat:<code>:date:<ISO>'; мульти-категории входят в каждую свою категорию.
"""

from datetime import date, timedelta

from loguru import logger
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from bot.handlers.calendar import (
    MONTHS_RU, build_range_calendar, format_period_ru, parse_nav_target, plural_events,
)
from bot.handlers.events import EVENTS_CITY, show_current_event
from bot.handlers.reviews import is_admin_user
from bot.middleware.stats import update_user_stats
from bot.utils.categories import (
    MAIN_MENU_CODES, category_dates_sql, counts_sql, get_category, horizon_days, period_count_sql, rest_categories,
)
from bot.utils.messenger import edit_or_send
from db.settings import resolve_category_mode
from shared.tz import belgrade_today

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
    mode = await resolve_category_mode(pool, update.effective_user.id)
    logger.info(f"CATEGORY | user_id={update.effective_user.id} | code={code} | mode={mode}")
    if mode == 'window':
        # режим window: категория сразу открывает список на сегодня; «Сегодня / Завтра / На дату» внизу того же экрана
        from bot.handlers.category_window import try_show_window
        today = belgrade_today()
        if await try_show_window(update, context, code, today, today):
            return
    # режим cards (или аварийный откат окна): окно выбора дня, как раньше
    await _render_window(update, context, code)


async def _open_filter(update: Update, context: ContextTypes.DEFAULT_TYPE, filter_name: str, force_cards: bool = False) -> bool:
    """Открыть день или период категории. Режим window: список-окно (пустой день тоже показывается окном, True).
    Режим cards или ошибка окна: карточки; False — событий нет (ничего не меняем, окно выбора дня остаётся)."""
    from bot.handlers.category_window import parse_cat_filter, try_show_window
    from bot.handlers.events import load_event_ids

    pool = context.bot_data.get('pool')
    user_id = update.effective_user.id
    parsed = None if force_cards else parse_cat_filter(filter_name)
    if parsed and await resolve_category_mode(pool, user_id) == 'window':
        if await try_show_window(update, context, *parsed):
            return True
    context.user_data.pop('window_filter', None)
    event_ids = await load_event_ids(pool, user_id, filter_name)
    logger.info(f"CATEGORY_FEED | user_id={user_id} | filter={filter_name} | items={len(event_ids)}")
    if not event_ids:
        return False

    context.user_data['event_ids'] = event_ids
    context.user_data['current_index'] = 0
    context.user_data['current_filter'] = filter_name
    context.user_data['expanded'] = False
    await show_current_event(update, context, pool)
    return True


async def _open_day(update: Update, context: ContextTypes.DEFAULT_TYPE, code: str, day: date) -> bool:
    """Лента категории на день."""
    return await _open_filter(update, context, f'cat:{code}:date:{day.isoformat()}')


async def handle_category_day(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """«Сегодня» / «Завтра» в окне категории: catd:<code>:today|tomorrow."""
    query = update.callback_query
    parts = query.data.split(':')
    if len(parts) != 3 or get_category(parts[1]) is None or parts[2] not in ('today', 'tomorrow'):
        await query.answer('Неверный запрос', show_alert=True)
        return
    code, which = parts[1], parts[2]
    day = belgrade_today() + timedelta(days=1 if which == 'tomorrow' else 0)
    if not await _open_day(update, context, code, day):
        await query.answer('На этот день в категории событий нет', show_alert=True)
        return
    await query.answer()


def _parse_dates(parts: list[str]) -> list[date] | None:
    try:
        return [date.fromisoformat(x) for x in parts]
    except ValueError:
        return None


async def _count_period(context: ContextTypes.DEFAULT_TYPE, user_id: int, code: str, first: date, last: date) -> int:
    pool = context.bot_data.get('pool')
    return await pool.fetchval(
        period_count_sql(), user_id, EVENTS_CITY, horizon_days(), list(get_category(code).db_values), first, last,
    ) or 0


async def _show_category_calendar(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    code: str,
    start: date | None = None,
    end: date | None = None,
) -> None:
    """Календарь категории. start/end — выбранные день или период (None — ничего не выбрано)."""
    pool = context.bot_data.get('pool')
    category = get_category(code)
    user_id = update.effective_user.id
    if start is not None:
        year, month = start.year, start.month
        context.user_data['calendar_year'], context.user_data['calendar_month'] = year, month
    else:
        year = context.user_data.get('calendar_year', belgrade_today().year)
        month = context.user_data.get('calendar_month', belgrade_today().month)

    row = await pool.fetchrow(
        category_dates_sql(), user_id, EVENTS_CITY, horizon_days(), list(category.db_values))
    active = set(row['dates'] or [])
    max_date = row['max_date'] if row and row['max_date'] else belgrade_today()

    keyboard = build_range_calendar(
        year=year, month=month, active=active, max_date=max_date, code=code, start=start, end=end,
    )
    head = f'📅 {category.emoji} {category.label}\n\n'
    if start is None:
        text = f'{head}Выберите дату или начало периода:\n{MONTHS_RU[month]} {year}'
    else:
        last = end or start
        count = await _count_period(context, user_id, code, start, last)
        what = format_period_ru(start, last)
        if end is None:
            hint = 'Выберите конец периода или нажмите «Показать».'
        else:
            hint = 'Нажмите «Показать» или выберите другие даты.'
        text = f'{head}{what}: {plural_events(count)}\n{hint}'
        keyboard.append([
            InlineKeyboardButton(f'Показать {count}', callback_data=f'catgo:{code}:{start.isoformat()}:{last.isoformat()}'),
            InlineKeyboardButton('Сбросить', callback_data=f'catrst:{code}'),
        ])
    keyboard.append([InlineKeyboardButton('⬅️ Назад', callback_data=f'cat:{code}')])

    await edit_or_send(
        context=context,
        chat_id=update.effective_chat.id,
        text=text,
        keyboard=InlineKeyboardMarkup(keyboard),
        parse_mode=None,
    )


async def handle_category_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Нажатие на день в календаре категории: catpick:<code>:<ISO> (выбрать начало)
    или catpick:<code>:<ISO>:<начало> (выбрать конец периода; то же число — открыть ленту этого дня)."""
    query = update.callback_query
    parts = query.data.split(':')
    if len(parts) not in (3, 4) or get_category(parts[1]) is None:
        await query.answer('Неверный запрос', show_alert=True)
        return
    days = _parse_dates(parts[2:])
    if days is None:
        await query.answer('Неверная дата', show_alert=True)
        return
    code, day = parts[1], days[0]
    start = days[1] if len(days) == 2 else None

    if start is not None and day == start:
        if not await _open_day(update, context, code, day):
            await query.answer('На этот день событий нет', show_alert=True)
            return
        await query.answer()
        return

    if start is not None and (day.year, day.month) == (start.year, start.month):
        first, last = sorted((start, day))
        if await _count_period(context, update.effective_user.id, code, first, last) == 0:
            await query.answer('В этом периоде событий нет', show_alert=True)
            return
        await query.answer()
        await _show_category_calendar(update, context, code, first, last)
        return

    if await _count_period(context, update.effective_user.id, code, day, day) == 0:
        await query.answer('На этот день событий нет', show_alert=True)
        return
    await query.answer()
    await _show_category_calendar(update, context, code, day)


async def handle_category_go(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """«Показать» под календарём: catgo:<code>:<ISO>:<ISO> (первый и последний день)."""
    query = update.callback_query
    parts = query.data.split(':')
    days = _parse_dates(parts[2:]) if len(parts) == 4 else None
    if days is None or get_category(parts[1]) is None or days[1] < days[0]:
        await query.answer('Неверный запрос', show_alert=True)
        return
    code, first, last = parts[1], days[0], days[1]
    if first == last:
        opened = await _open_day(update, context, code, first)
    else:
        opened = await _open_filter(update, context, f'cat:{code}:range:{first.isoformat()}:{last.isoformat()}')
    if not opened:
        await query.answer('Событий нет', show_alert=True)
        return
    await query.answer()


async def handle_category_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """«Сбросить»: catrst:<code> — календарь без выбора в том же месяце."""
    query = update.callback_query
    code = query.data.split(':', 1)[1] if ':' in query.data else ''
    if get_category(code) is None:
        await query.answer('Неизвестная категория', show_alert=True)
        return
    await query.answer()
    await _show_category_calendar(update, context, code)


async def handle_category_calendar_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """«К календарю» на карточке: catback:<code>:<ISO>:<ISO> — календарь с тем же периодом (один день — без выбора)."""
    query = update.callback_query
    parts = query.data.split(':')
    days = _parse_dates(parts[2:]) if len(parts) == 4 else None
    if days is None or get_category(parts[1]) is None or days[1] < days[0]:
        await query.answer('Неверный запрос', show_alert=True)
        return
    await query.answer()
    code, first, last = parts[1], days[0], days[1]
    context.user_data['calendar_year'], context.user_data['calendar_month'] = first.year, first.month
    if first == last or (first.year, first.month) != (last.year, last.month):
        await _show_category_calendar(update, context, code)
    else:
        await _show_category_calendar(update, context, code, first, last)


async def handle_calendar_noop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Неактивные кнопки календаря (дни без событий, заголовок, пустые места): подтверждаем нажатие.
    catnoop:prev / catnoop:next — стрелка на границе, показываем подсказку."""
    hints = {'catnoop:prev': 'Других месяцев нет', 'catnoop:next': 'Других месяцев нет'}
    await update.callback_query.answer(hints.get(update.callback_query.data))


async def handle_category_calendar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """«На дату» в окне категории: catcal:<code>."""
    query = update.callback_query
    code = query.data.split(':', 1)[1] if ':' in query.data else ''
    if get_category(code) is None:
        await query.answer('Неизвестная категория', show_alert=True)
        return
    await query.answer()
    today = belgrade_today()
    context.user_data['calendar_year'] = today.year
    context.user_data['calendar_month'] = today.month
    await _show_category_calendar(update, context, code)


async def handle_category_calendar_nav(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Листание месяцев по кругу: catcal_nav:<code>:<ГГГГ-ММ>[:wp|wn] (прежние prev|next тоже понимаем)."""
    query = update.callback_query
    parts = query.data.split(':')
    year = context.user_data.get('calendar_year', belgrade_today().year)
    month = context.user_data.get('calendar_month', belgrade_today().month)
    target = parse_nav_target(parts[2:], year, month) if len(parts) >= 3 else None
    if target is None or get_category(parts[1]) is None:
        await query.answer('Неверный запрос', show_alert=True)
        return
    year, month, toast = target
    await query.answer(toast)
    context.user_data['calendar_year'] = year
    context.user_data['calendar_month'] = month
    await _show_category_calendar(update, context, parts[1])
