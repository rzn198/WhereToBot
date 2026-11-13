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
from bot.formatters.event_card import build_event_card
from bot.utils.categories import feed_sql, get_category, horizon_days
from bot.utils.refs import Ref, event_ref, is_event_ref, ref_id
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


# Единая лента: посты каналов (messages) и события сайтов (events).
# Порядок внутри дня: сначала посты, потом события. События показываем только
# по Белграду (events.city = 'Belgrade').
EVENTS_CITY = 'Belgrade'


def _rows_to_refs(rows) -> list[Ref]:
    return [event_ref(r['id']) if r['kind'] == 'e' else r['id'] for r in rows]


async def load_event_ids(
    pool: asyncpg.Pool,
    user_id: int,
    filter_name: str,
) -> list[Ref]:
    """Список элементов ленты: int (пост) или 'e<id>' (событие)."""
    if filter_name == 'favorites':
        rows = await pool.fetch(f"""
            SELECT kind, id FROM (
                SELECT 0 AS ord, 'm' AS kind, m.surrogate_id AS id,
                       m.date AS d, m.created_at AS c
                FROM {Tables.MESSAGES} m
                JOIN {Tables.FAVORITES} f ON f.msg_id = m.surrogate_id
                WHERE f.user_id = $1
                AND m.hidden_by_admin = FALSE
                AND (m.date IS NULL OR m.date >= CURRENT_DATE)
                AND m.surrogate_id NOT IN (
                    SELECT msg_id FROM {Tables.HIDDEN_EVENTS} WHERE user_id = $1
                )
                UNION ALL
                SELECT 1, 'e', e.id, e.starts_at, e.created_at
                FROM {Tables.EVENTS} e
                JOIN {Tables.EVENT_FAVORITES} ef ON ef.event_id = e.id
                WHERE ef.user_id = $1
                AND e.starts_at >= CURRENT_DATE
                AND e.id NOT IN (
                    SELECT event_id FROM {Tables.EVENT_HIDDEN} WHERE user_id = $1
                )
            ) u
            ORDER BY d, ord, c, id
        """, user_id)
        return _rows_to_refs(rows)

    if filter_name.startswith('cat:'):
        # лента категории: посты и события от сегодня до горизонта, мульти-категории входят в каждую
        # 'cat:<code>' — вся категория, 'cat:<code>:date:<ISO>' — категория на один день
        code, _, rest = filter_name[4:].partition(':')
        category = get_category(code)
        if category is None:
            return []
        if rest.startswith('date:'):
            try:
                day = date.fromisoformat(rest[5:])
            except ValueError:
                return []
            rows = await pool.fetch(
                feed_sql(day=True), user_id, EVENTS_CITY, horizon_days(), list(category.db_values), day
            )
        else:
            rows = await pool.fetch(
                feed_sql(), user_id, EVENTS_CITY, horizon_days(), list(category.db_values)
            )
        return _rows_to_refs(rows)

    if filter_name.startswith('date:'):
        from datetime import date as date_type
        date_from = date_to = date_type.fromisoformat(filter_name.split(':', 1)[1])
    else:
        date_from, date_to = get_date_range(filter_name)

    rows = await pool.fetch(f"""
        SELECT kind, id FROM (
            SELECT 0 AS ord, 'm' AS kind, surrogate_id AS id,
                   date AS d, created_at AS c
            FROM {Tables.MESSAGES}
            WHERE date BETWEEN $1 AND $2
            AND hidden_by_admin = FALSE
            AND surrogate_id NOT IN (
                SELECT msg_id FROM {Tables.HIDDEN_EVENTS} WHERE user_id = $3
            )
            UNION ALL
            SELECT 1, 'e', id, starts_at, created_at
            FROM {Tables.EVENTS}
            WHERE starts_at BETWEEN $1 AND $2
            AND city = $4
            AND hidden_by_admin = FALSE
            AND id NOT IN (
                SELECT event_id FROM {Tables.EVENT_HIDDEN} WHERE user_id = $3
            )
        ) u
        ORDER BY d, ord, c, id
    """, date_from, date_to, user_id, EVENTS_CITY)
    return _rows_to_refs(rows)


async def fetch_event_by_id(
    pool: asyncpg.Pool,
    ref: Ref,
    user_id: int,
) -> dict | None:
    """Элемент ленты по ссылке. Для поста в результате kind='m', для события kind='e'."""
    if is_event_ref(ref):
        row = await pool.fetchrow(f"""
            SELECT
                e.id, e.title, e.description, e.description_ru, e.starts_at,
                e.venue, e.city, e.images, e.image_file_id, e.source_url,
                EXISTS(
                    SELECT 1 FROM {Tables.EVENT_FAVORITES}
                    WHERE user_id = $2 AND event_id = e.id
                ) as is_fav
            FROM {Tables.EVENTS} e
            WHERE e.id = $1
        """, ref_id(ref), user_id)
        if not row:
            return None
        result = dict(row)
        result['kind'] = 'e'
        return result

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
    """, ref, user_id)

    if not row:
        return None

    result = dict(row)
    result['kind'] = 'm'

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

    ref = event_ids[current_index]
    event = await fetch_event_by_id(pool, ref, user_id)

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

    # внутри ленты категории на карточке есть кнопка «К категории»
    current_filter = str(context.user_data.get('current_filter', ''))
    category_code = current_filter[4:].split(':', 1)[0] if current_filter.startswith('cat:') else None
    is_admin = user_id == config['telegram']['admin_user_id']
    under_review = False
    if is_admin:
        from bot.handlers.reviews import is_under_review
        under_review = await is_under_review(pool, ref)

    if event['kind'] == 'e':
        card = build_event_card(event, expanded=expanded, preview_chars=preview_chars)
        keyboard = get_event_nav_keyboard(
            current_index=current_index,
            total=len(event_ids),
            surrogate_id=ref,
            is_favorite=fav,
            is_preview=card.is_preview,
            category_code=category_code,
            event_date=event.get('starts_at'),
            is_admin=is_admin,
            under_review=under_review,
        )
        await edit_or_send(
            context=context,
            chat_id=chat_id,
            text=card.text,
            keyboard=keyboard,
            photo=card.photo,
            entities=card.entities,
            disable_link_preview=True,
        )
        return

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
        surrogate_id=ref,
        is_favorite=fav,
        is_preview=is_preview,
        category_code=category_code,
        event_date=event.get('date'),
        is_admin=is_admin,
        under_review=under_review,
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
