from loguru import logger
from telethon.tl.types import Message


async def download_photo(client, message: Message) -> bytes | None:
    if not message.photo:
        return None

    try:
        photo_bytes = await client.download_media(
            message.photo,
            file=bytes,
        )
        logger.debug(
            f"PHOTO_DOWNLOADED | msg_id={message.id} | "
            f"size={len(photo_bytes)} bytes"
        )
        return photo_bytes

    except Exception as e:
        logger.error(f"PHOTO_DOWNLOAD_ERROR | msg_id={message.id} | {e}")
        return None