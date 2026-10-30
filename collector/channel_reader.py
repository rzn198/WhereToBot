import json
import asyncpg
from datetime import date, datetime, timedelta, timezone
from loguru import logger
from telegram.error import TelegramError
from telethon import TelegramClient
from telethon.errors import RPCError
from telethon.tl.types import Message

from collector.parsers.zarko_tusic import ZarkoTusicParser
from collector.parsers.running_beg import RunningBegParser
from collector.categorizer_v2 import categorize
from shared.config import config
from db.tables import Tables
from collector.parsers.base import ParsedEvent
from collector.parsers.default import DefaultParser


PARSER_REGISTRY: dict[str, type] = {
    'default': DefaultParser,
    'zarko_tusic': ZarkoTusicParser,
    'running_beg': RunningBegParser,
}


def get_parser_for_channel(channel: str):
    custom = config.get('channels', {}).get('custom', [])
    for entry in custom:
        if entry['name'].lstrip('@') == channel.lstrip('@'):
            parser_name = entry['parser']
            parser_class = PARSER_REGISTRY.get(parser_name, DefaultParser)
            logger.debug(f"Using parser '{parser_name}' for {channel}")
            return parser_class()
    return DefaultParser()


def _retention_days() -> tuple[int, int] | None:
    cfg = config.get('cleanup') or {}
    try:
        return (
            int(cfg['event_cleanup_days']),
            int(cfg['no_date_cleanup_days']),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _is_expired(
    event_date,
    published_at: datetime,
    now: datetime,
    event_cleanup_days: int,
    no_date_cleanup_days: int,
) -> bool:
    if isinstance(event_date, datetime):
        event_date = event_date.date()
    if isinstance(event_date, date):
        return event_date < now.date() - timedelta(days=event_cleanup_days)
    if event_date is None:
        return published_at < now - timedelta(days=no_date_cleanup_days)
    return False

async def fetch_new_messages(
    client: TelegramClient,
    channel: str,
    pool: asyncpg.Pool,
    bot=None,
    photo_enabled: bool = False,
    limit: int = 100,
) -> int:
    from collector.photo.downloader import download_photo
    from collector.photo.uploader import upload_photo_to_storage

    parser = get_parser_for_channel(channel)
    saved = 0
    skipped_expired = 0
    retention = _retention_days()
    if retention is None:
        logger.warning(
            f"RETENTION_UNKNOWN | channel={channel} | "
            "config.cleanup incomplete: expired events are NOT skipped"
        )
    now = datetime.now(timezone.utc)

    try:
        entity = await client.get_entity(channel)
        messages = await client.get_messages(entity, limit=limit)
        logger.info(f"Fetched {len(messages)} messages from {channel}")
    except RPCError as e:
        logger.error(f"Telegram API error fetching {channel}: {e}")
        return 0
    except (ConnectionError, OSError) as e:
        logger.error(f"Network error fetching {channel}: {e}")
        return 0

    for message in messages:
        if not isinstance(message, Message) or not message.message:
            continue

        try:
            events = parser.parse(message)
        except Exception as e:
            logger.error(
                f"PARSE_ERROR | channel={channel} msg_id={message.id} | {e}"
            )
            continue

        published_at = message.date.replace(tzinfo=timezone.utc)

        for event in events:
            if retention is not None and _is_expired(
                event.date, published_at, now, *retention,
            ):
                skipped_expired += 1
                continue

            try:
                try:
                    channel_id = abs(message.peer_id.channel_id)
                except TelegramError:
                    channel_id = 0

                event_types = categorize(event.text, channel, position=event.position)

                result = await _save_event(
                    pool, message, channel, event,
                    channel_id=channel_id,
                    event_types=event_types,
                )

                if not result:
                    continue

                saved += 1

                if (
                    photo_enabled
                    and bot is not None
                    and event.has_photo
                    and len(event.text) <= config['photo']['caption_max_length']
                ):
                    photo_bytes = await download_photo(client, message)
                    if photo_bytes:
                        file_id, storage_msg_id = await upload_photo_to_storage(
                            bot, photo_bytes, message.id
                        )

                        if file_id:
                            await _update_photo(
                                pool=pool,
                                msg_id=message.id,
                                position=event.position,
                                photo_file_id=file_id,
                                storage_message_id=storage_msg_id,
                            )

            except Exception as e:
                logger.error(
                    f"DB_ERROR | channel={channel} "
                    f"msg_id={message.id} pos={event.position} | {e}"
                )

    logger.info(
        f"Saved {saved} new events from {channel} "
        f"(skipped_expired={skipped_expired})"
    )
    return saved


async def _save_event(
    pool: asyncpg.Pool,
    message: Message,
    channel: str,
    event: ParsedEvent,
    channel_id: int,
    event_types: list[str] | None = None,
) -> bool:
    published_at = message.date.replace(tzinfo=timezone.utc)

    query = f"""
        INSERT INTO {Tables.MESSAGES} (
            id, position, channel_id, text, entities,
            date, published_at, photo_file_id, storage_message_id,
            event_types
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
        ON CONFLICT (id, position) DO NOTHING
        RETURNING surrogate_id
    """

    result = await pool.fetchrow(
        query,
        message.id,
        event.position,
        channel_id,
        event.text,
        json.dumps(event.entities, ensure_ascii=False),
        event.date,
        published_at,
        event.photo_file_id,
        event.storage_message_id,
        event_types or [],
    )

    return result is not None


async def _update_photo(
    pool: asyncpg.Pool,
    msg_id: int,
    position: int,
    photo_file_id: str,
    storage_message_id: int | None,
) -> None:
    await pool.execute(
        f"""
        UPDATE {Tables.MESSAGES}
        SET photo_file_id = $1,
            storage_message_id = $2
        WHERE id = $3 AND position = $4
        """,
        photo_file_id,
        storage_message_id,
        msg_id,
        position,
    )
    logger.debug(
        f"PHOTO_SAVED | msg_id={msg_id} | pos={position} | "
        f"file_id={photo_file_id[:20]}..."
    )
