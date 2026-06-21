from loguru import logger
from telegram import Update
from telegram.ext import ContextTypes
from shared.config import config


async def handle_donate(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    user = update.effective_user
    logger.info(f"DONATE | user_id={user.id if user else '?'}")

    donate_cfg = config.get('donate', {})
    url = donate_cfg.get('url', '')

    if url:
        await update.message.reply_text(
            f"Поддержать проект можно здесь:\n{url}"
        )
    else:
        await update.message.reply_text(
            "Раздел в разработке. Скоро появится! 🙏"
        )