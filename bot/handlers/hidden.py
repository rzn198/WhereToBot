from loguru import logger
from telegram import Update
from telegram.ext import ContextTypes
from db.tables import Tables
from bot.handlers.start import show_main_menu
from bot.handlers.events import show_current_event


async def handle_hide(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    await query.answer()

    pool = context.bot_data.get('pool')
    user_id = update.effective_user.id
    data = query.data

    parts = data.split(':')
    if len(parts) != 2 or not parts[1].isdigit():
        await query.answer('Неверный запрос', show_alert=True)
        return

    surrogate_id = int(parts[1])

    try:
        await pool.execute(f"""
            INSERT INTO {Tables.HIDDEN_EVENTS} (user_id, msg_id)
            VALUES ($1, $2)
            ON CONFLICT DO NOTHING
        """, user_id, surrogate_id)

        logger.info(
            f"HIDE | user_id={user_id} | surrogate_id={surrogate_id}"
        )
    except Exception as e:
        logger.error(f"HIDE error: {e}")
        await query.answer('Ошибка. Попробуй ещё раз.', show_alert=True)
        return

    event_ids = context.user_data.get('event_ids', [])
    current_index = context.user_data.get('current_index', 0)

    if surrogate_id in event_ids:
        event_ids.remove(surrogate_id)
        context.user_data['event_ids'] = event_ids

        if current_index >= len(event_ids):
            context.user_data['current_index'] = max(0, len(event_ids) - 1)

    if not event_ids:
        await show_main_menu(update, context)
        return

    await show_current_event(update, context, pool)