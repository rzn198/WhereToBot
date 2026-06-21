# Formatting of events
# Converting Entitys for JSONB → MessageEntity for Bot API

import json
from loguru import logger
from telegram import Bot, MessageEntity, InlineKeyboardMarkup
from telegram.error import TelegramError, BadRequest

from collector.entities.utf16 import utf16_len
from shared.config import config


TYPE_MAP = {
    'bold':              MessageEntity.BOLD,
    'italic':            MessageEntity.ITALIC,
    'underline':         MessageEntity.UNDERLINE,
    'strikethrough':     MessageEntity.STRIKETHROUGH,
    'code':              MessageEntity.CODE,
    'pre':               MessageEntity.PRE,
    'texturl':           MessageEntity.TEXT_LINK,      # Telethon: MessageEntityTextUrl
    'mentionname':       MessageEntity.TEXT_MENTION,   # Telethon: MessageEntityMentionName
    'spoiler':           MessageEntity.SPOILER,
    'blockquote':        MessageEntity.BLOCKQUOTE,
    'url':               MessageEntity.URL,
    'mention':           MessageEntity.MENTION,
    'hashtag':           MessageEntity.HASHTAG,
    'cashtag':           MessageEntity.CASHTAG,
    'botcommand':        MessageEntity.BOT_COMMAND,
    'email':             MessageEntity.EMAIL,
    'phone':             MessageEntity.PHONE_NUMBER,
}


def build_entities(
    text: str,
    raw_entities: list[dict] | str | None,
) -> list[MessageEntity]:

    if not raw_entities:
        return []

    if isinstance(raw_entities, str):
        try:
            raw_entities = json.loads(raw_entities)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse entities JSON: {e}")
            return []

    if not isinstance(raw_entities, list):
        return []

    text_len = utf16_len(text)
    result = []

    for entity in raw_entities:
        try:
            entity_type = entity.get('_', '').replace('MessageEntity', '').lower()
            offset = entity.get('offset', 0)
            length = entity.get('length', 0)

            if offset < 0 or length <= 0:
                logger.debug(f"Skipping entity: invalid offset/length | {entity}")
                continue

            if offset + length > text_len:
                logger.debug(
                    f"Skipping entity: out of bounds | "
                    f"offset={offset} length={length} text_len={text_len}"
                )
                continue

            tg_type = TYPE_MAP.get(entity_type)
            if not tg_type:
                logger.debug(f"Skipping entity: unknown type '{entity_type}'")
                continue

            kwargs = {'offset': offset, 'length': length}

            if entity_type == 'texturl':
                url = entity.get('url')
                if not url:
                    continue
                kwargs['url'] = url

            result.append(MessageEntity(type=tg_type, **kwargs))

        except Exception as e:
            logger.warning(f"Failed to build entity: {e} | entity={entity}")
            continue

    return result


async def send_event(
    bot: Bot,
    chat_id: int,
    text: str,
    raw_entities: list[dict] | str | None,
    photo_file_id: str | None = None,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:

    entities = build_entities(text, raw_entities)

    if photo_file_id:
        cfg = config.get('photo', {})
        caption_limit = cfg.get('caption_max_length', 1024)

        if utf16_len(text) <= caption_limit:
            try:
                await bot.send_photo(
                    chat_id=chat_id,
                    photo=photo_file_id,
                    caption=text,
                    caption_entities=entities,
                    reply_markup=reply_markup,
                )
                return
            except BadRequest as e:
                logger.warning(
                    f"Photo send failed (file_id invalid?): {e} | "
                    f"chat_id={chat_id} file_id={photo_file_id}"
                )
            except TelegramError as e:
                logger.error(f"Telegram error sending photo: {e}")

    try:
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            entities=entities,
            reply_markup=reply_markup,
        )
    except TelegramError as e:
        logger.error(f"Failed to send message: {e} | chat_id={chat_id}")