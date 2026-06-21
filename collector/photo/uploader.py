import asyncio
from io import BytesIO
from loguru import logger
from telegram import Bot
from telegram.error import TelegramError, RetryAfter

from shared.config import config


async def upload_photo_to_storage(
    bot: Bot,
    photo_bytes: bytes,
    msg_id: int,
) -> tuple[str | None, int | None]:
    storage_channel_id = config['telegram']['storage_channel']
    rate_limit = config['photo']['rate_limit_seconds']

    try:
        sent = await bot.send_photo(
            chat_id=storage_channel_id,
            photo=BytesIO(photo_bytes),
        )
        file_id = sent.photo[-1].file_id
        storage_message_id = sent.message_id

        logger.info(
            f"PHOTO_UPLOADED | msg_id={msg_id} | "
            f"file_id={file_id[:20]}... | "
            f"storage_msg_id={storage_message_id}"
        )
        await asyncio.sleep(rate_limit)
        return file_id, storage_message_id

    except RetryAfter as e:
        logger.warning(
            f"PHOTO_FLOOD | msg_id={msg_id} | "
            f"retry_after={e.retry_after}s"
        )
        await asyncio.sleep(e.retry_after + 1)
        try:
            sent = await bot.send_photo(
                chat_id=storage_channel_id,
                photo=BytesIO(photo_bytes),
            )
            file_id = sent.photo[-1].file_id
            storage_message_id = sent.message_id
            logger.info(f"PHOTO_UPLOADED_RETRY | msg_id={msg_id}")
            return file_id, storage_message_id
        except TelegramError as e2:
            logger.error(f"PHOTO_UPLOAD_ERROR_RETRY | msg_id={msg_id} | {e2}")
            return None, None

    except TelegramError as e:
        logger.error(f"PHOTO_UPLOAD_ERROR | msg_id={msg_id} | {e}")
        return None, None