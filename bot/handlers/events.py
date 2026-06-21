import json
import asyncpg
from datetime import date, timedelta
from loguru import logger
from telegram import Update, MessageEntity
from telegram.ext import ContextTypes
from collector.entities.truncator import truncate_if_needed, EventText
from collector.entities.utf16 import utf16_len
from db.tables import Tables
from bot.keyboards.event_nav import get_event_nav_keyboard
from bot.formatters.event import build_entities
from bot.middleware.stats import update_user_stats
from bot.utils.messenger import edit_or_send
from shared.config import config


def get_date_range(filter_name: str) -> tuple[date, date]:
    today = date.today()
    if filter_name == 'today':
        return today, today
    elif filter_name == 'tomorrow':
        tomorrow = today + timedelta(days=1)
        return tomorrow, tomorrow
    elif filter_name == 'week':
        return today, today + timedelta(days=7)
    elif filter_name == 'month':
        return today, today + timedelta(days=30)
    return today, today + timedelta(days=30)


async def load_event_ids(
    pool: asyncpg.Pool,
    user_id: int,
    filter_name: str,
) -> list[int]:
    if filter_name == 'favorites':
        rows = await pool.fetch(f"""
                SELECT m.surrogate_id
                FROM {Tables.MESSAGES} m
                JOIN {Tables.FAVORITES} f ON f.msg_id = m.surrogate_id
                WHERE f.user_id = $1
                AND m.hidden_by_admin = FALSE
                AND (m.date IS NULL OR m.date >= CURRENT_DATE)
                AND m.surrogate_id NOT IN (
                    SELECT msg_id FROM {Tables.HIDDEN_EVENTS} WHERE user_id = $1
                )
                ORDER BY m.date, m.created_at
            """, user_id)

    elif filter_name.startswith('date:'):
        from datetime import date as date_type
        date_str = filter_name.split(':', 1)[1]
        selected_date = date_type.fromisoformat(date_str)
        rows = await pool.fetch(f"""
            SELECT surrogate_id FROM {Tables.MESSAGES}
            WHERE date = $1
            AND hidden_by_admin = FALSE
            AND surrogate_id NOT IN (
                SELECT msg_id FROM {Tables.HIDDEN_EVENTS} WHERE user_id = $2
            )
            ORDER BY created_at
        """, selected_date, user_id)

    else:
        date_from, date_to = get_date_range(filter_name)
        rows = await pool.fetch(f"""
            SELECT surrogate_id FROM {Tables.MESSAGES}
            WHERE date BETWEEN $1 AND $2
            AND hidden_by_admin = FALSE
            AND surrogate_id NOT IN (
                SELECT msg_id FROM {Tables.HIDDEN_EVENTS} WHERE user_id = $3
            )
            ORDER BY date, created_at
        """, date_from, date_to, user_id)

    return [r['surrogate_id'] for r in rows]


async def fetch_event_by_id(
    pool: asyncpg.Pool,
    surrogate_id: int,
    user_id: int,
) -> dict | None:
    row = await pool.fetchrow(f"""
        SELECT
            m.surrogate_id, m.text, m.entities, m.date,
            m.photo_file_id, m.storage_message_id,
            EXISTS(
                SELECT 1 FROM {Tables.FAVORITES}
                WHERE user_id = $2 AND msg_id = m.surrogate_id
            ) as is_fav
        FROM {Tables.MESSAGES} m
        WHERE m.surrogate_id = $1
    """, surrogate_id, user_id)

    if not row:
        return None

    result = dict(row)

    entities = result.get('entities')
    if isinstance(entities, str):
        result['entities'] = json.loads(entities)
    elif entities is None:
        result['entities'] = []

    return result


async def show_current_event(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    pool: asyncpg.Pool,
) -> None:

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    event_ids = context.user_data.get('event_ids', [])
    current_index = context.user_data.get('current_index', 0)
    expanded = context.user_data.get('expanded', False)

    if not event_ids:
        await edit_or_send(
            context=context,
            chat_id=chat_id,
            text='Событий не найдено 🤷',
            keyboard=_get_back_to_menu_keyboard(),
        )
        return

    surrogate_id = event_ids[current_index]
    event = await fetch_event_by_id(pool, surrogate_id, user_id)

    if not event:
        await edit_or_send(
            context=context,
            chat_id=chat_id,
            text='Событие не найдено.',
            keyboard=_get_back_to_menu_keyboard(),
        )
        return

    fav = event['is_fav']
    preview_chars = config.get('ui', {}).get('preview_chars', 300)
    full_text = event['text']
    photo_file_id = event.get('photo_file_id')
    has_photo = bool(photo_file_id)

    text_too_long = len(full_text) > preview_chars
    needs_preview = (text_too_long or has_photo) and not expanded

    if needs_preview:
        raw_entities = event['entities'] or []

        truncated = truncate_if_needed(
            EventText(text=full_text, entities=raw_entities),
            limit=preview_chars,
        )

        suffix_content = 'Полное описание события⬇️⬇️⬇️📋'
        suffix_text = '\n\n' + suffix_content
        display_text = truncated.text + suffix_text

        suffix_offset = utf16_len(truncated.text + '\n\n')
        suffix_length = utf16_len(suffix_content)

        base_entities = build_entities(truncated.text, truncated.entities)

        italic_entity = MessageEntity(
            type=MessageEntity.ITALIC,
            offset=suffix_offset,
            length=suffix_length,
        )

        display_entities = base_entities + [italic_entity]
        display_photo = None
        is_preview = True

    else:
        display_text = full_text
        display_entities = build_entities(full_text, event['entities'] or [])
        display_photo = photo_file_id
        is_preview = False

    keyboard = get_event_nav_keyboard(
        current_index=current_index,
        total=len(event_ids),
        surrogate_id=surrogate_id,
        is_favorite=fav,
        is_preview=is_preview,
    )

    await edit_or_send(
        context=context,
        chat_id=chat_id,
        text=display_text,
        keyboard=keyboard,
        photo=display_photo,
        entities=display_entities,
        disable_link_preview=is_preview,
    )


def _get_back_to_menu_keyboard():
    from telegram import InlineKeyboardMarkup, InlineKeyboardButton
    return InlineKeyboardMarkup([[
        InlineKeyboardButton('🏠 В меню', callback_data='nav:menu')
    ]])


async def handle_filter(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    await query.answer()

    pool = context.bot_data.get('pool')
    user_id = update.effective_user.id

    data = query.data  # 'filter:today' или 'calendar_date:2026-05-15'

    if data.startswith('calendar_date:'):
        date_str = data.split(':', 1)[1]
        filter_name = f'date:{date_str}'
    else:
        filter_name = data.split(':')[1]

    await update_user_stats(pool, update)
    logger.info(f"FILTER | user_id={user_id} | filter={filter_name}")

    event_ids = await load_event_ids(pool, user_id, filter_name)

    context.user_data['event_ids'] = event_ids
    context.user_data['current_index'] = 0
    context.user_data['current_filter'] = filter_name

    await show_current_event(update, context, pool)
