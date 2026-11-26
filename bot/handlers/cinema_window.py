"""Киноафиша в виде окна (режим new): список фильмов на день, карточка фильма, описание, сеансы. Без состояния.

Всё нужное лежит в callback_data и ссылках (работает после перезапуска бота, ничего не хранится в user_data):
    cl:<день>[:<стр>]            список фильмов дня (день ГГГГ-ММ-ДД), листание страниц по кругу; из календаря приходит без страницы
    cld:today | cld:tomorrow      «Сегодня» / «Завтра» (день считается в момент нажатия)
    clc                           календарь одного дня (активны только даты с сеансами);  clcn:<ГГГГ-ММ>[:wp|wn]  соседний месяц
    cm:<фильм>:<день>[:wp|wn]     карточка фильма; карусель по списку дня (алфавит), wp/wn — переход через край
    cmd:<фильм>:<день>            полное описание
    cms:<фильм>:<день>            сеансы фильма в этот день;  cmp:<фильм>:<день>:<№>:<ЧЧММ>  один сеанс («Локация», «Билеты»)
Строки списка — ссылки на карточку (t.me/<бот>?start=cm_<фильм>_<день>): /start убирает команду и список и присылает карточку
новым сообщением внизу, как в окне категорий. Данные берутся из cinema_movies (названия, перевод, жанры от AI) и cinema_events.
"""
from __future__ import annotations

import functools
import time
from datetime import date, datetime, timedelta
from html import escape

from loguru import logger
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from bot.handlers.calendar import MONTHS_RU, build_calendar, parse_nav_target
from bot.keyboards.carousel import carousel_row
from bot.utils import cinema_text as ct
from bot.utils.links import cinema_card_url
from bot.utils.messenger import edit_or_send
from bot.utils.window import format_day_title, truncate, window_settings
from db import cinema_favorites
from db.tables import Tables
from shared.tz import BELGRADE_TZ, belgrade_today

NOOP = 'catnoop'
MAX_DAYS_AHEAD = 400
WRAP_TOASTS = {'wp': '🔁 К последнему фильму', 'wn': '🔁 К первому фильму'}


# ---------------------------------------------------------------- данные
async def load_day_films(pool, day: date, now: datetime | None = None) -> list[dict]:
    """Фильмы со сеансами в этот день (сегодня — только будущие), по алфавиту. В каждом sessions — число сеансов."""
    start, end = ct.day_bounds(day, now)
    rows = await pool.fetch(
        f"""SELECT m.id, m.title_serbian, m.title_original, m.title_en, m.title_ru, m.genres_ru, count(*) AS sessions
            FROM {Tables.CINEMA_EVENTS} e JOIN {Tables.CINEMA_MOVIES} m ON m.id = e.movie_key
            WHERE e.showtime >= $1 AND e.showtime < $2
            GROUP BY m.id""", start, end)
    return ct.sort_films([dict(r) for r in rows])


async def load_movie(pool, movie_id: int) -> dict | None:
    row = await pool.fetchrow(
        f"""SELECT id, title_serbian, title_original, title_en, title_ru, genres_ru, description_src, description_ru
            FROM {Tables.CINEMA_MOVIES} WHERE id = $1""", movie_id)
    return dict(row) if row else None


async def load_poster(pool, movie_id: int) -> str | None:
    """Ссылка на постер на сайте кинотеатра (сами постеры не храним): по приоритету источников."""
    url = await pool.fetchval(
        f"""SELECT movie_poster_url FROM {Tables.CINEMA_EVENTS}
            WHERE movie_key = $1 AND movie_poster_url IS NOT NULL AND movie_poster_url <> ''
            ORDER BY COALESCE(array_position($2::text[], source::text), 99), showtime LIMIT 1""",
        movie_id, list(ct.POSTER_SOURCES))
    return url if isinstance(url, str) and url.startswith('http') else None


async def load_sessions(pool, movie_id: int, day: date, now: datetime | None = None) -> list[dict]:
    start, end = ct.day_bounds(day, now)
    rows = await pool.fetch(
        f"""SELECT cinema_name, cinema_maps_url, showtime, ticket_url FROM {Tables.CINEMA_EVENTS}
            WHERE movie_key = $1 AND showtime >= $2 AND showtime < $3
            ORDER BY showtime, cinema_name, ticket_url""", movie_id, start, end)
    return [dict(r) for r in rows]


async def load_calendar_dates(pool) -> tuple[set[date], date]:
    row = await pool.fetchrow(
        f"""SELECT array_agg(DISTINCT DATE(showtime AT TIME ZONE 'Europe/Belgrade')) AS dates
            FROM {Tables.CINEMA_EVENTS} WHERE showtime >= now() AND movie_key IS NOT NULL""")
    dates = set(row['dates'] or []) if row else set()
    return dates, (max(dates) if dates else belgrade_today())


# ---------------------------------------------------------------- разбор callback_data
def parse_day(token: str) -> date | None:
    try:
        day = date.fromisoformat(token)
    except (TypeError, ValueError):
        return None
    today = belgrade_today()
    return day if today - timedelta(days=1) <= day <= today + timedelta(days=MAX_DAYS_AHEAD) else None


def parse_movie_args(parts: list[str]) -> tuple[int, date] | None:
    """[<фильм>, <день>, ...] → (фильм, день)."""
    if len(parts) < 2 or not parts[0].isdigit():
        return None
    day = parse_day(parts[1])
    return (int(parts[0]), day) if day else None


async def bot_username(context: ContextTypes.DEFAULT_TYPE) -> str | None:
    from bot.notifications.push import bot_username as resolve
    return await resolve(context.bot)


# ---------------------------------------------------------------- устойчивость
async def _alert_admin(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    from bot.handlers.category_window import _alert_admin as alert      # общий лимит: не чаще раза в 10 минут
    await alert(context, text)


def guarded(fn):
    """Сбой одного экрана не должен оставлять пользователя без ответа: пишем в лог, предупреждаем админа, показываем сообщение."""
    @functools.wraps(fn)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            await fn(update, context)
        except Exception as e:
            logger.exception(f'CINEMA_UI | ошибка в {fn.__name__}')
            await _alert_admin(context, f'⚠️ Киноафиша (новая): ошибка {type(e).__name__}: {e}\nПодробности в логе бота (CINEMA_UI).')
            try:
                await edit_or_send(
                    context=context, chat_id=update.effective_chat.id, text='⚠️ Не получилось показать афишу. Попробуйте ещё раз.',
                    keyboard=InlineKeyboardMarkup([[InlineKeyboardButton('🏠 В меню', callback_data='nav:menu')]]), parse_mode=None)
            except Exception:
                logger.warning('CINEMA_UI | не показать сообщение об ошибке')
    return wrapper


# ---------------------------------------------------------------- клавиатуры
def _day_row(day: date) -> list[InlineKeyboardButton]:
    """Переключатель дня; выбранный день отмечен точкой и неактивен (повторное нажатие дало бы «Message is not modified»)."""
    today = belgrade_today()
    tomorrow = today + timedelta(days=1)
    return [
        InlineKeyboardButton('• 📅 Сегодня' if day == today else '📅 Сегодня', callback_data=NOOP if day == today else 'cld:today'),
        InlineKeyboardButton('• 📅 Завтра' if day == tomorrow else '📅 Завтра', callback_data=NOOP if day == tomorrow else 'cld:tomorrow'),
        InlineKeyboardButton('📅 На дату', callback_data='clc'),
    ]


def _pager_row(day: date, page: int, pages: int) -> list[InlineKeyboardButton]:
    base = f'cl:{day.isoformat()}'
    return [
        InlineKeyboardButton('◀️', callback_data=f'{base}:{pages if page == 1 else page - 1}'),
        InlineKeyboardButton(f'{page}/{pages}', callback_data=NOOP),
        InlineKeyboardButton('▶️', callback_data=f'{base}:{1 if page == pages else page + 1}'),
    ]


def list_keyboard(day: date, page: int, pages: int, link_less: list[dict] | None = None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for f in link_less or []:           # запасной вариант без username бота: кнопки вместо ссылок
        rows.append([InlineKeyboardButton(ct.list_line(f, 40), callback_data=f'cm:{f["id"]}:{day.isoformat()}')])
    if pages > 1:
        rows.append(_pager_row(day, page, pages))
    rows.append(_day_row(day))
    rows.append([InlineKeyboardButton('⬅️ Назад', callback_data='nav:menu')])
    return InlineKeyboardMarkup(rows)


def card_keyboard(film: dict, day: date, films: list[dict], index: int, page_size: int) -> InlineKeyboardMarkup:
    total = len(films)
    iso = day.isoformat()
    prev_f, next_f = films[(index - 1) % total], films[(index + 1) % total]
    rows: list[list[InlineKeyboardButton]] = []
    arrows = carousel_row(
        index, total,
        f'cm:{prev_f["id"]}:{iso}' + (':wp' if index == 0 else ''),
        f'cm:{next_f["id"]}:{iso}' + (':wn' if index == total - 1 else ''))
    if arrows:
        rows.append(arrows)
    if ct.has_more_description(film):
        rows.append([InlineKeyboardButton('📖 Полное описание', callback_data=f'cmd:{film["id"]}:{iso}')])
    rows.append([InlineKeyboardButton('🕐 Выбери время', callback_data=f'cms:{film["id"]}:{iso}')])
    rows.append([InlineKeyboardButton('⬅️ Назад', callback_data=f'cl:{iso}:{ct.page_of(index, page_size)}'),
                 InlineKeyboardButton('🏠 В меню', callback_data='nav:menu')])
    return InlineKeyboardMarkup(rows)


# ---------------------------------------------------------------- экраны
async def show_list(update: Update, context: ContextTypes.DEFAULT_TYPE, day: date, page: int = 1) -> None:
    t0 = time.perf_counter()
    pool = context.bot_data.get('pool')
    today = belgrade_today()
    st = window_settings()
    films = await load_day_films(pool, day)
    t1 = time.perf_counter()
    pages = ct.page_count(len(films), st.page_size)
    page = ct.clamp_page(page, pages)
    chat_id = update.effective_chat.id

    if not films:
        text = f'🎬 Киноафиша · {format_day_title(day, today)}\n\n{ct.empty_text(day, today)}'
        await edit_or_send(context=context, chat_id=chat_id, text=text, keyboard=list_keyboard(day, 1, 1), parse_mode=None)
        logger.info(f'CINEMA_LIST | user_id={update.effective_user.id} | {day} | films=0')
        return

    shown = films[(page - 1) * st.page_size: page * st.page_size]
    username = await bot_username(context)
    head = escape(ct.list_head(day, today, len(films), page, pages))
    lines = [head, '']
    for f in shown:
        line = escape(ct.list_line(f, st.title_max))
        lines.append(f'<a href="{cinema_card_url(username, f["id"], day)}">{line}</a>' if username else line)
    if not username:
        logger.warning('CINEMA_LIST | у бота нет username: вместо ссылок кнопки')
    await edit_or_send(context=context, chat_id=chat_id, text='\n'.join(lines), parse_mode='HTML', disable_link_preview=True,
                       keyboard=list_keyboard(day, page, pages, None if username else shown))
    context.user_data.pop('window_filter', None)
    logger.info(f'CINEMA_LIST | user_id={update.effective_user.id} | {day} | films={len(films)} | page={page}/{pages} '
                f'| sql_ms={(t1 - t0) * 1000:.0f} | total_ms={(time.perf_counter() - t0) * 1000:.0f}')


async def show_card(update: Update, context: ContextTypes.DEFAULT_TYPE, movie_id: int, day: date) -> bool:
    """Карточка фильма в карусели дня. False — фильма нет в списке этого дня (показан список дня)."""
    pool = context.bot_data.get('pool')
    films = await load_day_films(pool, day)
    index = next((i for i, f in enumerate(films) if f['id'] == movie_id), None)
    movie = await load_movie(pool, movie_id) if index is not None else None
    if movie is None:
        await show_list(update, context, day)
        return False
    poster = await load_poster(pool, movie_id)
    await edit_or_send(
        context=context, chat_id=update.effective_chat.id, text=ct.card_text(movie), parse_mode='HTML', photo=poster,
        keyboard=card_keyboard(movie, day, films, index, window_settings().page_size))
    logger.info(f'CINEMA_CARD | user_id={update.effective_user.id} | movie={movie_id} | {day} | {index + 1}/{len(films)}')
    return True


async def open_afisha(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопка «Афиша кинотеатров» в режиме new: сразу список на сегодня."""
    await show_list(update, context, belgrade_today())


async def open_cinema_card(update: Update, context: ContextTypes.DEFAULT_TYPE, movie_id: int, day: date) -> bool:
    """/start cm_...: карточка по ссылке из списка. Команда и меню удаляются, карточка приходит новым сообщением внизу."""
    from bot.handlers.push_links import drop_menu
    chat_id = update.effective_chat.id
    message = update.message
    await drop_menu(context, chat_id, update.effective_user.id,
                    [message.message_id if message else None, context.user_data.get('menu_message_id')])
    await show_card(update, context, movie_id, day)
    return True


# ---------------------------------------------------------------- обработчики
@guarded
async def handle_cinema_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """cl:<день>[:<стр>]"""
    query = update.callback_query
    parts = query.data.split(':')[1:]
    day = parse_day(parts[0]) if parts else None
    try:
        page = int(parts[1]) if len(parts) > 1 else 1
    except ValueError:
        page = 0
    if day is None or page < 1:
        await query.answer('Неверный запрос', show_alert=True)
        return
    await query.answer()
    await show_list(update, context, day, page)


@guarded
async def handle_cinema_day(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """cld:today | cld:tomorrow"""
    query = update.callback_query
    await query.answer()
    day = belgrade_today() + (timedelta(days=1) if query.data.endswith('tomorrow') else timedelta())
    await show_list(update, context, day)


async def _show_calendar(update: Update, context: ContextTypes.DEFAULT_TYPE, year: int, month: int) -> None:
    pool = context.bot_data.get('pool')
    dates, max_date = await load_calendar_dates(pool)
    keyboard = build_calendar(year=year, month=month, dates_with_events=dates, max_date=max_date,
                              callback_prefix='cl', nav_prefix='clcn')
    keyboard.append([InlineKeyboardButton('⬅️ Назад', callback_data='cld:today')])
    await edit_or_send(
        context=context, chat_id=update.effective_chat.id, parse_mode=None,
        text=f'📅 Киноафиша — выберите дату:\n\n{MONTHS_RU[month]} {year}', keyboard=InlineKeyboardMarkup(keyboard))


@guarded
async def handle_cinema_calendar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()
    today = belgrade_today()
    await _show_calendar(update, context, today.year, today.month)


@guarded
async def handle_cinema_calendar_nav(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """clcn:<ГГГГ-ММ>[:wp|wn]"""
    query = update.callback_query
    today = belgrade_today()
    target = parse_nav_target(query.data.split(':')[1:], today.year, today.month)
    if target is None:
        await query.answer()
        return
    year, month, toast = target
    await query.answer(toast)
    await _show_calendar(update, context, year, month)


@guarded
async def handle_cinema_card(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """cm:<фильм>:<день>[:wp|wn]"""
    query = update.callback_query
    parts = query.data.split(':')[1:]
    args = parse_movie_args(parts)
    if args is None:
        await query.answer('Неверный запрос', show_alert=True)
        return
    await query.answer(WRAP_TOASTS.get(parts[2]) if len(parts) > 2 else None)
    await show_card(update, context, *args)


@guarded
async def handle_cinema_desc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """cmd:<фильм>:<день>"""
    query = update.callback_query
    args = parse_movie_args(query.data.split(':')[1:])
    if args is None:
        await query.answer('Неверный запрос', show_alert=True)
        return
    await query.answer()
    movie_id, day = args
    pool = context.bot_data.get('pool')
    movie = await load_movie(pool, movie_id)
    if movie is None:
        await show_list(update, context, day)
        return
    await edit_or_send(
        context=context, chat_id=update.effective_chat.id, text=ct.description_screen_text(movie), parse_mode='HTML',
        photo=await load_poster(pool, movie_id),
        keyboard=InlineKeyboardMarkup([[InlineKeyboardButton('⬅️ Назад', callback_data=f'cm:{movie_id}:{day.isoformat()}')]]))


async def _show_sessions(update: Update, context: ContextTypes.DEFAULT_TYPE, movie_id: int, day: date) -> None:
    pool = context.bot_data.get('pool')
    movie = await load_movie(pool, movie_id)
    if movie is None:
        await show_list(update, context, day)
        return
    sessions = await load_sessions(pool, movie_id, day)
    back = InlineKeyboardButton('⬅️ Назад', callback_data=f'cm:{movie_id}:{day.isoformat()}')
    poster = await load_poster(pool, movie_id)
    if not sessions:
        await edit_or_send(context=context, chat_id=update.effective_chat.id, text='🤷 Сеансов не найдено.', photo=poster,
                           keyboard=InlineKeyboardMarkup([[back]]), parse_mode=None)
        return
    rows = [[InlineKeyboardButton(ct.session_label(s['showtime'], s['cinema_name']),
                                  callback_data=f"cmp:{movie_id}:{day.isoformat()}:{i}:{s['showtime'].astimezone(BELGRADE_TZ):%H%M}")]
            for i, s in enumerate(sessions)]
    rows.append([back])
    await edit_or_send(
        context=context, chat_id=update.effective_chat.id, parse_mode='HTML', photo=poster, keyboard=InlineKeyboardMarkup(rows),
        text=f'🎬 <b>{escape(ct.display_title(movie))}</b>\n\n🕐 Выберите сеанс:')


@guarded
async def handle_cinema_sessions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """cms:<фильм>:<день>"""
    query = update.callback_query
    args = parse_movie_args(query.data.split(':')[1:])
    if args is None:
        await query.answer('Неверный запрос', show_alert=True)
        return
    await query.answer()
    await _show_sessions(update, context, *args)


async def _show_pick(update: Update, context: ContextTypes.DEFAULT_TYPE, movie: dict, day: date, sessions: list[dict], index: int) -> None:
    """Экран одного сеанса: «Локация», «Билеты», «Напомнить / Удалить»."""
    pool = context.bot_data.get('pool')
    s = sessions[index]
    movie_id = movie['id']
    iso = day.isoformat()
    hhmm = f"{s['showtime'].astimezone(BELGRADE_TZ):%H%M}"
    buttons = []
    if (s['cinema_maps_url'] or '').startswith('http'):
        buttons.append(InlineKeyboardButton('📍 Локация', url=s['cinema_maps_url']))
    if (s['ticket_url'] or '').startswith('http'):
        buttons.append(InlineKeyboardButton('🎟 Билеты', url=s['ticket_url']))
    is_fav = await cinema_favorites.exists(pool, update.effective_user.id, movie_id, s['cinema_name'], s['showtime'])
    fav = InlineKeyboardButton('💔 Удалить' if is_fav else '❤️ Напомнить',
                               callback_data=f"{'cfr' if is_fav else 'cfa'}:{movie_id}:{iso}:{index}:{hhmm}")
    rows = ([buttons] if buttons else []) + [[fav], [InlineKeyboardButton('⬅️ Назад', callback_data=f'cms:{movie_id}:{iso}')]]
    row = await pool.fetchrow(f'SELECT description FROM {Tables.CINEMA_DESCRIPTIONS} WHERE cinema_name = $1', s['cinema_name'])
    about = ((row['description'] if row else '') or '').strip()
    text = (f"🎬 <b>{escape(ct.display_title(movie))}</b>\n\n🏛 {escape(s['cinema_name'])}\n"
            f"🕐 {s['showtime'].astimezone(BELGRADE_TZ):%H:%M}")
    if about:
        text += f'\n\n{escape(about)}'
    await edit_or_send(context=context, chat_id=update.effective_chat.id, text=text, parse_mode='HTML',
                       photo=await load_poster(pool, movie_id), keyboard=InlineKeyboardMarkup(rows))


async def _pick_target(update: Update, context: ContextTypes.DEFAULT_TYPE, parts: list[str]):
    """cmp/cfa/cfr: [<фильм>, <день>, <№>, <ЧЧММ>] → (фильм, день, сеансы, №) либо None. Номер и время сверяются с БД:
    если афиша успела обновиться, показываем сеансы заново."""
    args = parse_movie_args(parts)
    if args is None or len(parts) < 4 or not parts[2].isdigit():
        await update.callback_query.answer('Неверный запрос', show_alert=True)
        return None
    movie_id, day = args
    pool = context.bot_data.get('pool')
    movie = await load_movie(pool, movie_id)
    sessions = await load_sessions(pool, movie_id, day)
    index = int(parts[2])
    if movie is None or index >= len(sessions) or f"{sessions[index]['showtime'].astimezone(BELGRADE_TZ):%H%M}" != parts[3]:
        await update.callback_query.answer()
        await _show_sessions(update, context, movie_id, day)
        return None
    return movie, day, sessions, index


@guarded
async def handle_cinema_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """cmp:<фильм>:<день>:<№ сеанса>:<ЧЧММ>"""
    target = await _pick_target(update, context, update.callback_query.data.split(':')[1:])
    if target is None:
        return
    await update.callback_query.answer()
    await _show_pick(update, context, *target)


@guarded
async def handle_cinema_fav_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """cfa|cfr:<фильм>:<день>:<№ сеанса>:<ЧЧММ> — «Напомнить» / «Удалить» на экране сеанса."""
    query = update.callback_query
    action, *parts = query.data.split(':')
    target = await _pick_target(update, context, parts)
    if target is None:
        return
    movie, day, sessions, index = target
    s = sessions[index]
    pool = context.bot_data.get('pool')
    user_id = update.effective_user.id
    if action == 'cfa':
        await cinema_favorites.add(pool, user_id, movie['id'], s['cinema_name'], s['showtime'])
        await query.answer('❤️ Добавлено в запланированное')
    else:
        await cinema_favorites.remove(pool, user_id, movie['id'], s['cinema_name'], s['showtime'])
        await query.answer('Убрано из запланированного')
    logger.info(f"CINEMA_FAV | {action} | user_id={user_id} | movie={movie['id']} | {s['cinema_name']} | {s['showtime']}")
    await _show_pick(update, context, movie, day, sessions, index)


# ---------------------------------------------------------------- «Кино» в запланированном
def _fav_title(fav: dict) -> str:
    return ct.display_title(fav)


def _fav_when(fav: dict, today: date) -> str:
    local = fav['showtime'].astimezone(BELGRADE_TZ)
    return f"{format_day_title(local.date(), today)}, {local:%H:%M}"


def favorites_list_content(favs: list[dict], today: date) -> tuple[str, InlineKeyboardMarkup]:
    rows = []
    lines = ['🎬 Кино в запланированном', '']
    for f in favs:
        lines.append(f"{'🎬' if f['alive'] else '❌'} {_fav_title(f)} · {_fav_when(f, today)} · {f['cinema_name']}" + ('' if f['alive'] else ' · сеанс снят'))
        label = f"{'🎬' if f['alive'] else '❌'} {truncate(_fav_title(f), 24)} · {f['showtime'].astimezone(BELGRADE_TZ):%d.%m %H:%M}"
        rows.append([InlineKeyboardButton(label, callback_data=f"cfo:{f['id']}")])
    if not favs:
        lines.append('Пока ничего нет 🤷\nСеанс можно добавить кнопкой «❤️ Напомнить» на его экране в афише кинотеатров.')
    rows.append([InlineKeyboardButton('⬅️ Назад', callback_data='filter:favorites'), InlineKeyboardButton('🏠 В меню', callback_data='nav:menu')])
    return '\n'.join(lines), InlineKeyboardMarkup(rows)


async def show_favorites_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    favs = await cinema_favorites.list_upcoming(context.bot_data.get('pool'), update.effective_user.id)
    text, keyboard = favorites_list_content(favs, belgrade_today())
    await edit_or_send(context=context, chat_id=update.effective_chat.id, text=text, keyboard=keyboard, parse_mode=None)


@guarded
async def handle_cinema_fav_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """cfl"""
    await update.callback_query.answer()
    await show_favorites_list(update, context)


def _fav_id(data: str) -> int | None:
    parts = data.split(':')
    return int(parts[1]) if len(parts) == 2 and parts[1].isdigit() else None


@guarded
async def handle_cinema_fav_open(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """cfo:<id избранного> — сеанс из списка: время, кинотеатр, «Локация», «Билеты», «Удалить»."""
    query = update.callback_query
    fav_id = _fav_id(query.data)
    if fav_id is None:
        await query.answer('Неверный запрос', show_alert=True)
        return
    await query.answer()
    pool = context.bot_data.get('pool')
    fav = await cinema_favorites.get(pool, update.effective_user.id, fav_id)
    if fav is None:
        await show_favorites_list(update, context)
        return
    today = belgrade_today()
    text = f"🎬 <b>{escape(_fav_title(fav))}</b>\n\n🏛 {escape(fav['cinema_name'])}\n🕐 {escape(_fav_when(fav, today))}"
    if not fav['alive']:
        text += '\n\n❌ Сеанс снят из афиши — в напоминании он не придёт.'
    links = []
    if fav['alive'] and (fav['maps_url'] or '').startswith('http'):
        links.append(InlineKeyboardButton('📍 Локация', url=fav['maps_url']))
    if fav['alive'] and (fav['ticket_url'] or '').startswith('http'):
        links.append(InlineKeyboardButton('🎟 Билеты', url=fav['ticket_url']))
    rows = ([links] if links else []) + [[InlineKeyboardButton('💔 Удалить', callback_data=f'cfd:{fav_id}')],
                                          [InlineKeyboardButton('⬅️ Назад', callback_data='cfl')]]
    await edit_or_send(context=context, chat_id=update.effective_chat.id, text=text, parse_mode='HTML',
                       photo=await load_poster(pool, fav['movie_id']) if fav['alive'] else None, keyboard=InlineKeyboardMarkup(rows))


@guarded
async def handle_cinema_fav_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """cfd:<id избранного>"""
    query = update.callback_query
    fav_id = _fav_id(query.data)
    if fav_id is None:
        await query.answer('Неверный запрос', show_alert=True)
        return
    await query.answer('Убрано из запланированного')
    await cinema_favorites.remove_by_id(context.bot_data.get('pool'), update.effective_user.id, fav_id)
    await show_favorites_list(update, context)
