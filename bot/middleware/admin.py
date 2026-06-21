from functools import wraps
from loguru import logger
from telegram import Update
from telegram.ext import ContextTypes
from shared.config import config


def admin_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user:
            return

        admin_id = config['telegram']['admin_user_id']

        if user.id != admin_id:
            logger.warning(
                f"ADMIN_ACCESS_DENIED | "
                f"user_id={user.id} | "
                f"username={user.username} | "
                f"handler={func.__name__}"
            )
            await update.message.reply_text(
                "У вас нет доступа к этой команде."
            )
            return

        logger.debug(
            f"ADMIN_ACCESS_GRANTED | "
            f"user_id={user.id} | "
            f"handler={func.__name__}"
        )
        return await func(update, context)

    return wrapper


async def check_is_admin(update: Update) -> bool:
    user = update.effective_user
    if not user:
        return False
    return user.id == config['telegram']['admin_user_id']