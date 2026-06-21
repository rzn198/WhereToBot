# bot/handlers/cinema.py
# UI киноафиши

from datetime import date, timedelta, date as date_type
from datetime import datetime
from shared.config import config
from loguru import logger
from zoneinfo import ZoneInfo
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from telegram.error import TelegramError

from db.tables import Tables
from bot.handlers.contact import escape_md
from bot.utils.messenger import edit_or_send
from shared.constants import MOVIE_DESC_SHORT_DISPLAY_LENGTH, MOVIE_DESC_FULL_DISPLAY_LENGTH

BELGRADE_TZ = ZoneInfo('Europe/Belgrade')


async def _get_unique_movies(pool, target_date: str) -> list[dict]:
    parsed_date = date_type.fromisoformat(target_date)
    today = date_type.today()

    if parsed_date == today:
        now_utc = datetime.now(tz=BELGRADE_TZ).astimezone(__import__('zoneinfo').ZoneInfo('UTC'))
        rows = await pool.fetch(f"""
            SELECT DISTINCT ON (movie_title_normalized)
                movie_id, movie_title, movie_title_serbian,
                movie_title_normalized,
                movie_genre, movie_description_short, movie_description_full,
                movie_poster_url, movie_poster_file_id
            FROM {Tables.CINEMA_EVENTS}
            WHERE DATE(showtime AT TIME ZONE 'Europe/Belgrade') = $1
            AND showtime > $2
            ORDER BY movie_title_normalized,
                CASE source WHEN 'cineplexx' THEN 1 ELSE 2 END,
                showtime
        """, parsed_date, now_utc)
    else:
        rows = await pool.fetch(f"""
            SELECT DISTINCT ON (movie_title_normalized)
                movie_id, movie_title, movie_title_serbian,
                movie_title_normalized,
                movie_genre, movie_description_short, movie_description_full,
                movie_poster_url, movie_poster_file_id
            FROM {Tables.CINEMA_EVENTS}
            WHERE DATE(showtime AT TIME ZONE 'Europe/Belgrade') = $1
            ORDER BY movie_title_normalized,
                CASE source WHEN 'cineplexx' THEN 1 ELSE 2 END,
                showtime
        """, parsed_date)

    return [dict(r) for r in rows]


async def _get_movie_sessions(pool, movie_title_normalized: str, target_date: str) -> list[dict]:
    parsed_date = date_type.fromisoformat(target_date)
    today = date_type.today()

    if parsed_date == today:
        now_utc = datetime.now(tz=BELGRADE_TZ).astimezone(__import__('zoneinfo').ZoneInfo('UTC'))
        rows = await pool.fetch(f"""
            SELECT cinema_name, cinema_maps_url, cinema_url,
                   showtime, ticket_url
            FROM {Tables.CINEMA_EVENTS}
            WHERE movie_title_normalized = $1
            AND DATE(showtime AT TIME ZONE 'Europe/Belgrade') = $2
            AND showtime > $3
            ORDER BY showtime
        """, movie_title_normalized, parsed_date, now_utc)
    else:
        rows = await pool.fetch(f"""
            SELECT cinema_name, cinema_maps_url, cinema_url,
                   showtime, ticket_url
            FROM {Tables.CINEMA_EVENTS}
            WHERE movie_title_normalized = $1
            AND DATE(showtime AT TIME ZONE 'Europe/Belgrade') = $2
            ORDER BY showtime
        """, movie_title_normalized, parsed_date)

    return [dict(r) for r in rows]


async def handle_cinema_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    await query.answer()

    chat_id = update.effective_chat.id

    context.user_data.pop('cinema_movies', None)
    context.user_data.pop('cinema_index', None)
    context.user_data.pop('cinema_date', None)
    context.user_data.pop('cinema_sessions', None)

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton('📅 Сегодня', callback_data='cinema:today'),
            InlineKeyboardButton('📅 Завтра', callback_data='cinema:tomorrow'),
        ],
        [
            InlineKeyboardButton('📅 На дату', callback_data='cinema:calendar'),
        ],
        [
            InlineKeyboardButton('🏠 В меню', callback_data='nav:menu'),
        ],
    ])

    await edit_or_send(
        context=context,
        chat_id=chat_id,
        text= "🎬 *Киноафиша Белграда*\n\nВыберите дату:",
        keyboard=keyboard,
    )


async def handle_cinema_filter(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    await query.answer()

    pool = context.bot_data.get('pool')
    chat_id = update.effective_chat.id
    data = query.data

    parts = data.split(':')
    filter_type = parts[1]

    if filter_type == 'today':
        target_date = str(date.today())
    elif filter_type == 'tomorrow':
        target_date = str(date.today() + timedelta(days=1))
    elif filter_type == 'date' and len(parts) > 2:
        target_date = parts[2]
    else:
        return

    movies = await _get_unique_movies(pool, target_date)

    if not movies:
        await edit_or_send(
            context=context,
            chat_id=chat_id,
            text=f"🎬 На {target_date} сеансов нет.",
            keyboard=InlineKeyboardMarkup([[
                InlineKeyboardButton('◀️ Назад', callback_data='cinema:menu')
            ]]),
        )
        return

    context.user_data['cinema_movies'] = movies
    context.user_data['cinema_index'] = 0
    context.user_data['cinema_date'] = target_date

    await _show_cinema_movie(update, context)


async def _show_cinema_movie(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    chat_id = update.effective_chat.id
    movies = context.user_data.get('cinema_movies', [])
    idx = context.user_data.get('cinema_index', 0)

    if not movies or idx >= len(movies):
        return

    movie = movies[idx]
    total = len(movies)

    title = escape_md(movie['movie_title'] or '')
    title_serbian = escape_md(movie['movie_title_serbian'] or '')
    genre = escape_md(movie['movie_genre'] or '')
    description = escape_md((movie['movie_description_short'] or '')[:MOVIE_DESC_SHORT_DISPLAY_LENGTH])

    text = f"🎬 *{title}*"
    if title_serbian and title_serbian != title:
        text += f"\n🇷🇸 {title_serbian}"
    if genre:
        text += f"\n🎭 {genre}"
    if description:
        text += f"\n\n{description}"

    nav_row = [
        InlineKeyboardButton(f'◀️ {idx}/{total}', callback_data='cinema:prev'),
        InlineKeyboardButton(f'{idx+1}/{total} ▶️', callback_data='cinema:next'),
    ]

    keyboard = InlineKeyboardMarkup([
        nav_row,
        [InlineKeyboardButton('📖 Полное описание', callback_data=f'cinema:desc:{idx}')],
        [InlineKeyboardButton('🕐 Выбери время', callback_data=f'cinema:sessions:{idx}')],
        [InlineKeyboardButton('◀️ Назад', callback_data='cinema:menu')],
    ])

    photo = movie.get('movie_poster_file_id') or movie.get('movie_poster_url')

    await edit_or_send(
        context=context,
        chat_id=chat_id,
        text=text,
        keyboard=keyboard,
        photo=photo,
    )


async def handle_cinema_desc(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    await query.answer()

    chat_id = update.effective_chat.id
    movies = context.user_data.get('cinema_movies', [])
    idx = int(query.data.split(':')[2])

    if not movies or idx >= len(movies):
        return

    movie = movies[idx]
    title = escape_md(movie['movie_title'] or '')
    description = escape_md((movie['movie_description_full'] or movie['movie_description_short'] or 'Описание недоступно.')[:MOVIE_DESC_FULL_DISPLAY_LENGTH])

    text = f"🎬 *{title}*\n\n{description}"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton('◀️ Назад', callback_data=f'cinema:back_to_movie:{idx}')],
    ])

    photo = movie.get('movie_poster_file_id') or movie.get('movie_poster_url')

    await edit_or_send(
        context=context,
        chat_id=chat_id,
        text=text,
        keyboard=keyboard,
        photo=photo,
    )


async def handle_cinema_back_to_movie(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    await query.answer()

    idx = int(query.data.split(':')[2])
    context.user_data['cinema_index'] = idx
    await _show_cinema_movie(update, context)


async def handle_cinema_nav(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    await query.answer()

    movies = context.user_data.get('cinema_movies', [])
    idx = context.user_data.get('cinema_index', 0)
    direction = query.data.split(':')[1]

    if direction == 'prev':
        if idx > 0:
            context.user_data['cinema_index'] = idx - 1
        else:
            await query.answer('🎬 Это первый фильм', show_alert=False)
            return
    elif direction == 'next':
        if idx < len(movies) - 1:
            context.user_data['cinema_index'] = idx + 1
        else:
            await query.answer('🎬 Больше фильмов нет', show_alert=False)
            return

    await _show_cinema_movie(update, context)


async def handle_cinema_sessions(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    await query.answer()

    pool = context.bot_data.get('pool')
    chat_id = update.effective_chat.id

    movies = context.user_data.get('cinema_movies', [])
    idx = context.user_data.get('cinema_index', 0)
    target_date = context.user_data.get('cinema_date', '')

    if not movies or idx >= len(movies):
        return

    movie = movies[idx]
    movie_title_normalized = movie['movie_title_normalized']
    photo = movie.get('movie_poster_file_id') or movie.get('movie_poster_url')

    sessions = await _get_movie_sessions(pool, movie_title_normalized, target_date)

    if not sessions:
        await edit_or_send(
            context=context,
            chat_id=chat_id,
            text="🤷 Сеансов не найдено.",
            keyboard=InlineKeyboardMarkup([[
                InlineKeyboardButton('◀️ Назад', callback_data=f'cinema:back_to_movie:{idx}')
            ]]),
            photo=photo,
        )
        return

    context.user_data['cinema_sessions'] = [
        {
            'cinema_name': s['cinema_name'],
            'cinema_maps_url': s['cinema_maps_url'],
            'ticket_url': s['ticket_url'],
            'showtime': s['showtime'].isoformat(),
        }
        for s in sessions
    ]

    session_buttons = []
    for i, s in enumerate(sessions):
        showtime_belgrade = s['showtime'].astimezone(BELGRADE_TZ)
        time_str = showtime_belgrade.strftime('%H:%M')
        label = f"{time_str} — {s['cinema_name']}"
        session_buttons.append([
            InlineKeyboardButton(label, callback_data=f'cinema:pick:{i}')
        ])

    session_buttons.append([
        InlineKeyboardButton('◀️ Назад', callback_data=f'cinema:back_to_movie:{idx}')
    ])

    title = escape_md(movie['movie_title'])
    text = f"🎬 *{title}*\n\n🕐 Выберите сеанс:"

    await edit_or_send(
        context=context,
        chat_id=chat_id,
        text=text,
        keyboard=InlineKeyboardMarkup(session_buttons),
        photo=photo,
    )


async def handle_cinema_pick(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    await query.answer()

    chat_id = update.effective_chat.id
    pool = context.bot_data.get('pool')

    sessions = context.user_data.get('cinema_sessions', [])
    movies = context.user_data.get('cinema_movies', [])
    movie_idx = context.user_data.get('cinema_index', 0)
    session_idx = int(query.data.split(':')[2])

    if session_idx >= len(sessions):
        return

    session = sessions[session_idx]
    cinema_name = session['cinema_name']
    maps_url = session['cinema_maps_url']
    ticket_url = session['ticket_url']

    showtime = datetime.fromisoformat(session['showtime'])
    showtime_belgrade = showtime.astimezone(BELGRADE_TZ)
    time_str = showtime_belgrade.strftime('%H:%M')

    buttons = []
    if maps_url and maps_url.startswith('http'):
        buttons.append(InlineKeyboardButton('📍 Локация', url=maps_url))
    if ticket_url and ticket_url.startswith('http'):
        buttons.append(InlineKeyboardButton('🎟 Билеты', url=ticket_url))

    keyboard = InlineKeyboardMarkup([
        buttons,
        [InlineKeyboardButton('◀️ Назад', callback_data=f'cinema:sessions:{movie_idx}')],
    ])

    movie = movies[movie_idx] if movie_idx < len(movies) else {}
    title = escape_md(movie.get('movie_title', ''))
    photo = movie.get('movie_poster_file_id') or movie.get('movie_poster_url')

    cinema_desc_row = await pool.fetchrow(f"""
        SELECT description FROM {Tables.CINEMA_DESCRIPTIONS}
        WHERE cinema_name = $1
    """, cinema_name)
    cinema_description = (cinema_desc_row['description'] or '').strip() if cinema_desc_row else ''

    text = (
        f"🎬 *{title}*\n\n"
        f"🏛 {escape_md(cinema_name)}\n"
        f"🕐 {time_str}"
    )
    if cinema_description:
        text += f"\n\n{cinema_description}"

    await edit_or_send(
        context=context,
        chat_id=chat_id,
        text=text,
        keyboard=keyboard,
        photo=photo,
    )


async def handle_cinema_calendar(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    await query.answer()

    pool = context.bot_data.get('pool')
    today = date.today()
    context.user_data['cinema_calendar_year'] = today.year
    context.user_data['cinema_calendar_month'] = today.month

    await _show_cinema_calendar(update, context, pool)


async def handle_cinema_calendar_nav(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    await query.answer()

    pool = context.bot_data.get('pool')
    direction = query.data.split(':')[1]

    year = context.user_data.get('cinema_calendar_year', date.today().year)
    month = context.user_data.get('cinema_calendar_month', date.today().month)

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

    context.user_data['cinema_calendar_year'] = year
    context.user_data['cinema_calendar_month'] = month

    await _show_cinema_calendar(update, context, pool)


async def _show_cinema_calendar(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    pool,
) -> None:
    from bot.handlers.calendar import build_calendar, MONTHS_RU

    chat_id = update.effective_chat.id

    year = context.user_data.get('cinema_calendar_year', date.today().year)
    month = context.user_data.get('cinema_calendar_month', date.today().month)

    row = await pool.fetchrow(f"""
        SELECT
            array_agg(DISTINCT DATE(showtime AT TIME ZONE 'Europe/Belgrade')) as dates,
            MAX(DATE(showtime AT TIME ZONE 'Europe/Belgrade')) as max_date
        FROM {Tables.CINEMA_EVENTS}
        WHERE DATE(showtime AT TIME ZONE 'Europe/Belgrade') >= CURRENT_DATE
    """)

    dates_with_events = set(row['dates'] or [])
    max_date = row['max_date'] if row and row['max_date'] else date.today()

    keyboard = build_calendar(
        year=year,
        month=month,
        dates_with_events=dates_with_events,
        max_date=max_date,
        callback_prefix='cinema:date',
        nav_prefix='cinema_cal_nav',
    )
    keyboard.append([InlineKeyboardButton('◀️ Назад', callback_data='cinema:menu')])

    await edit_or_send(
        context=context,
        chat_id=chat_id,
        text=f"📅 *Киноафиша — выберите дату:*\n\n{MONTHS_RU[month]} {year}",
        keyboard=InlineKeyboardMarkup(keyboard),
    )
