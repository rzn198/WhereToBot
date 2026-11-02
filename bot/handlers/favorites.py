from loguru import logger
from telegram import Update
from telegram.ext import ContextTypes

from db.tables import Tables
from bot.handlers.events import show_current_event
from bot.utils.refs import is_event_ref, parse_ref, ref_id


async def handle_favorites(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    await query.answer()

    pool = context.bot_data.get('pool')
    user_id = update.effective_user.id
    data = query.data

    parts = data.split(':')
    ref = parse_ref(parts[2]) if len(parts) == 3 else None
    if ref is None:
        await query.answer('Неверный запрос', show_alert=True)
        return

    action = parts[1]
    surrogate_id = ref_id(ref)
    is_event = is_event_ref(ref)
    table, column = (
        (Tables.EVENT_FAVORITES, 'event_id') if is_event
        else (Tables.FAVORITES, 'msg_id')
    )

    if action == 'add':
        try:
            await pool.execute(f"""
                INSERT INTO {table} (user_id, {column})
                VALUES ($1, $2)
                ON CONFLICT DO NOTHING
            """, user_id, surrogate_id)
            logger.info(
                f"FAV_ADD | user_id={user_id} | ref={ref}"
            )
            await query.answer('⭐ Добавлено в избранное', show_alert=False)
        except Exception as e:
            logger.error(f"FAV_ADD error: {e}")
            await query.answer('Ошибка. Попробуй ещё раз.', show_alert=True)

    elif action == 'remove':
        try:
            await pool.execute(f"""
                DELETE FROM {table}
                WHERE user_id = $1 AND {column} = $2
            """, user_id, surrogate_id)
            logger.info(
                f"FAV_REMOVE | user_id={user_id} | ref={ref}"
            )
            await query.answer('Убрано из избранного', show_alert=False)
        except Exception as e:
            logger.error(f"FAV_REMOVE error: {e}")
            await query.answer('Ошибка. Попробуй ещё раз.', show_alert=True)

    await show_current_event(update, context, pool)