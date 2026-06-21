import asyncio
import os
import sys

if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

os.environ.setdefault('APP_ENV', 'production')

from loguru import logger
from telegram import Bot as TelegramBot

from shared.logging import setup_logging
from shared.config import config
from db.connection import init_db_pool, close_db_pool
from db.validators import validate_config_tables
from collector.client import build_telethon_client
from collector.scheduler import start_scheduler
from collector.channel_reader import fetch_new_messages
from collector.photo.validator import verify_storage_channel_access


async def main():
    setup_logging('collector')
    logger.info(f"Collector starting | env={config['env']}")

    pool = await init_db_pool()

    await validate_config_tables(pool)

    bot = TelegramBot(token=config['telegram']['bot_token'])

    photo_enabled = await verify_storage_channel_access(bot)
    if not photo_enabled:
        logger.critical("Photo module disabled — bot is not admin in storage channel")

    client = build_telethon_client()
    await client.start()
    logger.info("Telethon client connected")

    scheduler = start_scheduler(pool, client, bot=bot)

    channels = config['telegram']['channels']
    for channel in channels:
        channel = channel.strip()
        if not channel:
            continue
        try:
            await fetch_new_messages(
                client, channel, pool,
                bot=bot,
                photo_enabled=photo_enabled,
            )
        except Exception as e:
            logger.error(f"INITIAL_FETCH_ERROR | channel={channel} | {e}")

    try:
        await client.run_until_disconnected()
    finally:
        scheduler.shutdown()
        await bot.close()
        await close_db_pool(pool)
        logger.info("Collector stopped")


if __name__ == '__main__':
    asyncio.run(main())