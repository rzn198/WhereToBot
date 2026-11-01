from loguru import logger
from telegram import Update
from telegram.ext import ContextTypes
from db.tables import Tables
from bot.handlers.start import show_main_menu
from bot.handlers.events import show_current_event
from bot.utils.refs import is_event_ref, parse_ref, ref_id


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
    ref = parse_ref(parts[1]) if len(parts) == 2 else None
    if ref is None:
        await query.answer('Неверный запрос', show_alert=True)
        return

    table, column = (
        (Tables.EVENT_HIDDEN, 'event_id') if is_event_ref(ref)
        else (Tables.HIDDEN_EVENTS, 'msg_id')
    )

    try:
        await pool.execute(f"""
            INSERT INTO {table} (user_id, {column})
            VALUES ($1, $2)
            ON CONFLICT DO NOTHING
        """, user_id, ref_id(ref))

        logger.info(
            f"HIDE | user_id={user_id} | ref={ref}"
        )
    except Exception as e:
        logger.error(f"HIDE error: {e}")
        await query.answer('Ошибка. Попробуй ещё раз.', show_alert=True)
        return

    event_ids = context.user_data.get('event_ids', [])
    current_index = context.user_data.get('current_index', 0)

    if ref in event_ids:
        event_ids.remove(ref)
        context.user_data['event_ids'] = event_ids

        if current_index >= len(event_ids):
            context.user_data['current_index'] = max(0, len(event_ids) - 1)

    if not event_ids:
        await show_main_menu(update, context)
        return

    await show_current_event(update, context, pool)