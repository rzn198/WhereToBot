from loguru import logger
from telegram import Update
from telegram.ext import ContextTypes
from telegram.error import TelegramError

from bot.handlers.start import show_main_menu


async def handle_alert_dismiss(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass

    chat_id = update.effective_chat.id
    message_id = query.message.message_id

    try:
        await context.bot.delete_message(
            chat_id=chat_id,
            message_id=message_id,
        )
        logger.debug(f"ALERT_DISMISSED | user_id={update.effective_user.id} | msg_id={message_id}")
    except TelegramError as e:
        logger.warning(f"ALERT_DISMISS_ERROR | {e}")

    await show_main_menu(update, context)


async def delete_alert_message(bot, chat_id: int, message_id: int) -> None:
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
        logger.debug(f"ALERT_AUTO_DELETED | chat_id={chat_id} | msg_id={message_id}")
    except TelegramError as e:
        logger.warning(f"ALERT_AUTO_DELETE_ERROR | {e}")
