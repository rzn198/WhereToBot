from loguru import logger
from telegram import Update
from telegram.ext import ContextTypes
from telegram.error import TelegramError
from bot.keyboards.main_menu import get_main_menu_inline, MENU_TEXT
from bot.middleware.stats import update_user_stats
from bot.utils.messenger import edit_or_send
from db.tables import Tables
from shared.config import config


async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    pool = context.bot_data.get('pool')
    user = update.effective_user
    await update_user_stats(pool, update)
    logger.info(f"START | user_id={user.id if user else '?'}")
    menu_message_id = context.user_data.get('menu_message_id')
    context.user_data.clear()
    if menu_message_id:
        context.user_data['menu_message_id'] = menu_message_id
    await show_main_menu(update, context)


async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    pool = context.bot_data.get('pool')
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id if update.effective_user else 0
    admin_id = config['telegram']['admin_user_id']
    is_admin = user_id == admin_id
    keyboard = get_main_menu_inline(is_admin=is_admin)

    is_start_command = (
        update.message is not None
        and update.message.text is not None
        and update.message.text.startswith('/start')
    )

    if is_start_command and pool and user_id:
        try:
            row = await pool.fetchrow(f"""
                SELECT menu_message_id FROM {Tables.USER_STATS}
                WHERE user_id = $1
            """, user_id)
            db_menu_message_id = row['menu_message_id'] if row else None

            if db_menu_message_id:
                try:
                    await context.bot.delete_message(
                        chat_id=chat_id,
                        message_id=db_menu_message_id,
                    )
                    logger.debug(f"START_DELETED_OLD_MENU | msg_id={db_menu_message_id}")
                except TelegramError as e:
                    logger.warning(f"Could not delete old menu message: {e}")

            context.user_data.pop('menu_message_id', None)
            context.user_data.pop('message_type', None)

        except Exception as e:
            logger.error(f"START_DB_ERROR | user_id={user_id} | {e}")

    await edit_or_send(
        context=context,
        chat_id=chat_id,
        text=MENU_TEXT,
        keyboard=keyboard,
        user_id=user_id,
    )

    new_menu_message_id = context.user_data.get('menu_message_id')
    if pool and user_id and new_menu_message_id:
        try:
            await pool.execute(f"""
                UPDATE {Tables.USER_STATS}
                SET menu_message_id = $1
                WHERE user_id = $2
            """, new_menu_message_id, user_id)
            logger.debug(f"START_SAVED_MENU_MSG_ID | user_id={user_id} | msg_id={new_menu_message_id}")
        except Exception as e:
            logger.error(f"START_SAVE_MSG_ID_ERROR | user_id={user_id} | {e}")
