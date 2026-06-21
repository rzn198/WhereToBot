from loguru import logger
from shared.config import config


async def verify_storage_channel_access(bot) -> bool:
    storage_channel_id = config['telegram']['storage_channel']

    if not storage_channel_id:
        logger.warning("STORAGE_CHANNEL_ID не задан — фото отключено")
        return False

    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(
            chat_id=storage_channel_id,
            user_id=me.id
        )

        if member.status != 'administrator':
            logger.critical(
                f"Bot is not admin in storage channel {storage_channel_id} "
                f"| status={member.status} | Photo module DISABLED"
            )
            return False

        logger.info(
            f"Storage channel OK | channel={storage_channel_id} | "
            f"bot_status={member.status}"
        )
        return True

    except Exception as e:
        logger.critical(f"Cannot access storage channel: {e} | Photo DISABLED")
        return False