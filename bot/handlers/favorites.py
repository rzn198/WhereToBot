from loguru import logger
from telegram import Update
from telegram.ext import ContextTypes

from db.tables import Tables
from bot.handlers.events import show_current_event


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
    if len(parts) != 3 or not parts[2].isdigit():
        await query.answer('Неверный запрос', show_alert=True)
        return

    action = parts[1]
    surrogate_id = int(parts[2])

    if action == 'add':
        try:
            await pool.execute(f"""
                INSERT INTO {Tables.FAVORITES} (user_id, msg_id)
                VALUES ($1, $2)
                ON CONFLICT DO NOTHING
            """, user_id, surrogate_id)
            logger.info(
                f"FAV_ADD | user_id={user_id} | surrogate_id={surrogate_id}"
            )
            await query.answer('⭐ Добавлено в избранное', show_alert=False)
        except Exception as e:
            logger.error(f"FAV_ADD error: {e}")
            await query.answer('Ошибка. Попробуй ещё раз.', show_alert=True)

    elif action == 'remove':
        try:
            await pool.execute(f"""
                DELETE FROM {Tables.FAVORITES}
                WHERE user_id = $1 AND msg_id = $2
            """, user_id, surrogate_id)
            logger.info(
                f"FAV_REMOVE | user_id={user_id} | surrogate_id={surrogate_id}"
            )
            await query.answer('Убрано из избранного', show_alert=False)
        except Exception as e:
            logger.error(f"FAV_REMOVE error: {e}")
            await query.answer('Ошибка. Попробуй ещё раз.', show_alert=True)

    await show_current_event(update, context, pool)