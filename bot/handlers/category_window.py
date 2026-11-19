"""Окно категории со списком событий (режим window): день или период, строки-ссылки на карточки, страницы по 15.

Заходим сюда, когда режим категорий = window для этого пользователя: нажатие на категорию (список на сегодня,
handle_category), «Сегодня», «Завтра», день или период из календаря (_open_filter в categories.py).
Каждая строка списка ссылка на карточку (t.me/<бот>?start=cw_...): Telegram присылает боту /start cw_..., бот убирает
команду и список и показывает карточку новым сообщением внизу (как ссылки из пуша), лента карточек = тот же список;
на карточке «📋 К списку» возвращает на нужную страницу. Кнопок-номеров нет.
Callback_data (без состояния, работает после перезапуска):
    wl:<code>:<с>:<по>[:<страница>]   показать окно, листание страниц по кругу, «К списку» с карточки
Один SQL на показ окна, AI не участвует. В лог пишутся замеры (WINDOW | ...).
"""
from __future__ import annotations

import time
from datetime import date, timedelta

from loguru import logger
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from bot.handlers.calendar import format_period_ru
from bot.utils.categories import MAIN_MENU_CODES, get_category, horizon_days, window_rows_sql
from bot.utils.links import window_card_url
from bot.utils.messenger import edit_or_send
from bot.utils.window import (
    DAY_BUTTONS_PER_ROW, Item, Layout, format_day_button, format_day_title, item_from_row, layout_window, render_text,
    window_settings,
)
from shared.config import config
from shared.tz import belgrade_today

MAX_PERIOD_DAYS = 30                 # как у ленты по периоду (load_event_ids)
ADMIN_ALERT_PAUSE_SECONDS = 600      # алерт админу об аварийном откате не чаще раза в 10 минут
_last_admin_alert = 0.0


def feed_filter(code: str, first: date, last: date) -> str:
    """Имя фильтра ленты для этого дня или периода (то же, что открывают карточки)."""
    if first == last:
        return f'cat:{code}:date:{first.isoformat()}'
    return f'cat:{code}:range:{first.isoformat()}:{last.isoformat()}'


def parse_cat_filter(filter_name: str) -> tuple[str, date, date] | None:
    """'cat:<code>:date:<ISO>' / 'cat:<code>:range:<ISO>:<ISO>' → (код, с, по); всё остальное (вся категория и т.п.) → None."""
    if not filter_name.startswith('cat:'):
        return None
    code, _, rest = filter_name[4:].partition(':')
    try:
        if rest.startswith('date:'):
            day = date.fromisoformat(rest[5:])
            return code, day, day
        if rest.startswith('range:'):
            first, last = (date.fromisoformat(x) for x in rest[6:].split(':'))
            return code, first, last
    except ValueError:
        return None
    return None


def back_callback(code: str) -> str:
    return 'nav:menu' if code in MAIN_MENU_CODES else 'cats:menu'


def window_head(code: str, first: date, last: date, today: date) -> str:
    category = get_category(code)
    when = format_day_title(first, today) if first == last else format_period_ru(first, last)
    return f'{category.emoji} {category.label} · {when}'


def empty_text(first: date, last: date, today: date) -> str:
    if first != last:
        return 'В этом периоде ничего нет 🤷'
    if first == today:
        return 'На сегодня ничего нет 🤷'
    if first == today + timedelta(days=1):
        return 'На завтра ничего нет 🤷'
    return f'На {format_day_title(first, today)} ничего нет 🤷'


def valid_args(code: str, first: date, last: date) -> bool:
    return get_category(code) is not None and first <= last and (last - first).days <= MAX_PERIOD_DAYS


def _day_row(code: str, first: date, last: date) -> list[InlineKeyboardButton]:
    """Переключатель дня на том же экране. Выбранный день отмечен точкой и неактивен (catnoop): повторное нажатие
    перерисовало бы то же сообщение, Telegram ответил бы «Message is not modified» и бот прислал бы дубль."""
    today = belgrade_today()
    single = first == last
    tomorrow = today + timedelta(days=1)
    return [
        InlineKeyboardButton('• 📅 Сегодня' if single and first == today else '📅 Сегодня',
                             callback_data='catnoop' if single and first == today else f'catd:{code}:today'),
        InlineKeyboardButton('• 📅 Завтра' if single and first == tomorrow else '📅 Завтра',
                             callback_data='catnoop' if single and first == tomorrow else f'catd:{code}:tomorrow'),
        InlineKeyboardButton('📅 На дату', callback_data=f'catcal:{code}'),
    ]


def _pager_row(code: str, first: date, last: date, page: int, pages: int) -> list[InlineKeyboardButton]:
    """◀️ 2/3 ▶️: листание по кругу (с первой страницы назад на последнюю, с последней вперёд на первую)."""
    base = f'wl:{code}:{first.isoformat()}:{last.isoformat()}'
    return [
        InlineKeyboardButton('◀️', callback_data=f'{base}:{pages if page == 1 else page - 1}'),
        InlineKeyboardButton(f'{page}/{pages}', callback_data='catnoop'),
        InlineKeyboardButton('▶️', callback_data=f'{base}:{1 if page == pages else page + 1}'),
    ]


def build_keyboard(code: str, first: date, last: date, layout: Layout) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if layout.mode == 'aggregate':
        buttons = [
            InlineKeyboardButton(format_day_button(d, n), callback_data=f'wl:{code}:{d.isoformat()}:{d.isoformat()}')
            for d, n in layout.day_counts
        ]
        rows += [buttons[i:i + DAY_BUTTONS_PER_ROW] for i in range(0, len(buttons), DAY_BUTTONS_PER_ROW)]
    elif layout.pages > 1:
        rows.append(_pager_row(code, first, last, layout.page, layout.pages))
    rows.append(_day_row(code, first, last))
    rows.append([InlineKeyboardButton('⬅️ Назад', callback_data=back_callback(code))])
    return InlineKeyboardMarkup(rows)


async def bot_username(context: ContextTypes.DEFAULT_TYPE) -> str | None:
    from bot.notifications.push import bot_username as resolve
    return await resolve(context.bot)


async def _alert_admin(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    """Сообщение админу об аварийном откате окна на карточки; не чаще раза в 10 минут."""
    global _last_admin_alert
    now = time.monotonic()
    if _last_admin_alert and now - _last_admin_alert < ADMIN_ALERT_PAUSE_SECONDS:
        return
    _last_admin_alert = now
    try:
        await context.bot.send_message(chat_id=config['telegram']['admin_user_id'], text=text)
    except Exception as e:                         # алерт не должен ломать ответ пользователю
        logger.warning(f'WINDOW | не отправить алерт админу: {type(e).__name__}: {e}')


async def load_items(pool, user_id: int, code: str, first: date, last: date) -> list[Item]:
    from bot.handlers.events import EVENTS_CITY
    rows = await pool.fetch(window_rows_sql(), user_id, EVENTS_CITY, horizon_days(), list(get_category(code).db_values), first, last)
    return [item_from_row(r) for r in rows]


async def render_window(update: Update, context: ContextTypes.DEFAULT_TYPE, code: str, first: date, last: date,
                        page: int = 1) -> Layout:
    """Собрать и показать окно. Исключения пробрасываются (их ловит try_show_window)."""
    pool = context.bot_data.get('pool')
    user_id = update.effective_user.id
    today = belgrade_today()
    st = window_settings()

    t0 = time.perf_counter()
    items = await load_items(pool, user_id, code, first, last)
    t1 = time.perf_counter()
    head = window_head(code, first, last, today)
    layout = layout_window(items, head, first, last, today, st, page)
    username = await bot_username(context)
    if layout.mode == 'empty':
        text, parse_mode = f'{head}\n\n{empty_text(first, last, today)}', None
    elif layout.mode == 'aggregate' or not username:
        if layout.mode == 'list':
            logger.warning('WINDOW | у бота нет username: строки без ссылок')
        text, parse_mode = render_text(layout)
    else:
        text, parse_mode = render_text(layout, lambda it: window_card_url(username, code, first, last, it.ref))
    t2 = time.perf_counter()

    await edit_or_send(context=context, chat_id=update.effective_chat.id, text=text,
                       keyboard=build_keyboard(code, first, last, layout), parse_mode=parse_mode, disable_link_preview=True)
    logger.info(
        f'WINDOW | user_id={user_id} | code={code} | {first}..{last} | mode={layout.mode} | items={layout.total} '
        f'| page={layout.page}/{layout.pages} | chars={len(text)} '
        f'| sql_ms={(t1 - t0) * 1000:.0f} | render_ms={(t2 - t1) * 1000:.1f} | total_ms={(time.perf_counter() - t0) * 1000:.0f}'
    )
    context.user_data.pop('window_filter', None)    # метка «карточка открыта из окна» ставится только при открытии по ссылке
    return layout


async def try_show_window(update: Update, context: ContextTypes.DEFAULT_TYPE, code: str, first: date, last: date,
                          page: int = 1) -> bool:
    """Показать окно. False — не получилось (ошибка записана в лог, админу алерт): вызывающий откатывается на карточки."""
    if not valid_args(code, first, last):
        return False
    try:
        await render_window(update, context, code, first, last, page)
        return True
    except Exception as e:
        logger.exception(f'WINDOW | ошибка показа окна, откат на карточки | code={code} | {first}..{last}')
        await _alert_admin(context, f'⚠️ Окно категорий: ошибка {type(e).__name__}: {e}\nПользователям показаны карточки. '
                                    f'Подробности в логе бота (WINDOW).')
        return False


def _parse_args(parts: list[str]) -> tuple[str, date, date, int] | None:
    """[code, с, по, страница?] → (код, с, по, страница) или None, если что-то неверно."""
    if len(parts) < 3:
        return None
    try:
        first, last = date.fromisoformat(parts[1]), date.fromisoformat(parts[2])
        page = int(parts[3]) if len(parts) > 3 else 1
    except ValueError:
        return None
    if not valid_args(parts[0], first, last) or page < 1:
        return None
    return parts[0], first, last, page


async def handle_window_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """wl:<code>:<с>:<по>[:<страница>] — окно (листание, день из сводки периода, «📋 К списку» с карточки)."""
    query = update.callback_query
    args = _parse_args(query.data.split(':')[1:])
    if args is None:
        await query.answer('Неверный запрос', show_alert=True)
        return
    await query.answer()
    if not await try_show_window(update, context, *args):
        from bot.handlers.categories import _open_filter
        code, first, last, _ = args
        await _open_filter(update, context, feed_filter(code, first, last), force_cards=True)


async def open_window_card(update: Update, context: ContextTypes.DEFAULT_TYPE, code: str, first: date, last: date,
                           ref: int | str) -> bool:
    """/start cw_...: карточка по ссылке из списка. Команда и список удаляются, карточка приходит новым сообщением внизу
    (как ссылки из пуша), лента карточек = тот же список. Записи уже нет в списке: показываем сам список. False — не обработано."""
    from bot.handlers.events import load_event_ids, open_filter
    from bot.handlers.push_links import drop_menu

    if not valid_args(code, first, last):
        return False
    chat_id, user_id = update.effective_chat.id, update.effective_user.id
    pool = context.bot_data.get('pool')
    message = update.message
    await drop_menu(context, chat_id, user_id, [message.message_id if message else None, context.user_data.get('menu_message_id')])
    filter_name = feed_filter(code, first, last)
    event_ids = await load_event_ids(pool, user_id, filter_name)
    found = ref in event_ids
    logger.info(f'WINDOW_OPEN | user_id={user_id} | filter={filter_name} | ref={ref} | found={found}')
    if not found:
        return await try_show_window(update, context, code, first, last)
    context.user_data['window_filter'] = filter_name       # на карточке появится «📋 К списку»
    await open_filter(update, context, filter_name, start_ref=ref)
    return True
