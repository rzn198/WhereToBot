"""Раздел «По категориям»: окно с категориями и лента выбранной категории."""

from loguru import logger
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from bot.handlers.events import EVENTS_CITY, show_current_event
from bot.middleware.stats import update_user_stats
from bot.utils.categories import CATEGORIES, counts_sql, get_category, horizon_days
from bot.utils.messenger import edit_or_send

MENU_TEXT = '🗂 Выберите категорию:'
EMPTY_TEXT = 'Пока нет событий по категориям 🤷'


async def load_category_counts(pool, user_id: int) -> list[tuple]:
    """[(Category, число элементов ленты)] для категорий, где есть что показать."""
    row = await pool.fetchrow(
        counts_sql(), user_id, EVENTS_CITY, horizon_days(),
        *[list(c.db_values) for c in CATEGORIES],
    )
    return [(c, row[f'c{i}']) for i, c in enumerate(CATEGORIES) if row[f'c{i}'] > 0]


def build_menu_keyboard(counts: list[tuple]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f'{c.emoji} {c.label} ({n})', callback_data=f'cat:{c.code}')]
        for c, n in counts
    ]
    rows.append([InlineKeyboardButton('⬅️ Назад', callback_data='nav:menu')])
    return InlineKeyboardMarkup(rows)


async def _render_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, pool) -> None:
    user_id = update.effective_user.id
    counts = await load_category_counts(pool, user_id)
    await edit_or_send(
        context=context,
        chat_id=update.effective_chat.id,
        text=MENU_TEXT if counts else EMPTY_TEXT,
        keyboard=build_menu_keyboard(counts),
        parse_mode=None,
    )


async def handle_categories_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопки «По категориям» (главное меню) и «К категориям» (карточка)."""
    query = update.callback_query
    await query.answer()
    pool = context.bot_data.get('pool')
    logger.info(f"CATEGORIES_MENU | user_id={update.effective_user.id}")
    await _render_menu(update, context, pool)


async def handle_category(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Выбор категории: строим ленту (посты + события, от сегодня вперёд) и открываем первую карточку."""
    from bot.handlers.events import load_event_ids

    query = update.callback_query
    pool = context.bot_data.get('pool')
    user_id = update.effective_user.id
    code = query.data.split(':', 1)[1] if ':' in query.data else ''

    category = get_category(code)
    if category is None:
        await query.answer('Неизвестная категория', show_alert=True)
        return

    await update_user_stats(pool, update)
    filter_name = f'cat:{code}'
    event_ids = await load_event_ids(pool, user_id, filter_name)
    logger.info(f"CATEGORY | user_id={user_id} | code={code} | items={len(event_ids)}")

    if not event_ids:
        # событий не осталось (например, всё скрыто) — возвращаем к списку категорий
        await query.answer('В этой категории пока нет событий', show_alert=True)
        await _render_menu(update, context, pool)
        return

    await query.answer()
    context.user_data['event_ids'] = event_ids
    context.user_data['current_index'] = 0
    context.user_data['current_filter'] = filter_name
    context.user_data['expanded'] = False

    await show_current_event(update, context, pool)
