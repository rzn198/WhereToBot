import shutil
import asyncpg
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from loguru import logger
from telethon import TelegramClient

from shared.config import config
from collector.channel_reader import fetch_new_messages


async def job_parse_channels(
    client: TelegramClient,
    pool: asyncpg.Pool,
    bot=None,
    photo_enabled: bool = False,
) -> None:
    channels = config['telegram']['channels']
    logger.info(f"Parse job started | channels={channels}")

    total = 0
    for channel in channels:
        channel = channel.strip()
        if not channel:
            continue
        try:
            saved = await fetch_new_messages(
                client, channel, pool,
                bot=bot,
                photo_enabled=photo_enabled,
            )
            total += saved
        except Exception as e:
            logger.error(f"PARSE_JOB_CHANNEL_ERROR | channel={channel} | {e}")

    logger.info(f"Parse job finished | total_saved={total}")

    try:
        from scripts.find_duplicates import find_and_mark_duplicates
        flagged = await find_and_mark_duplicates(pool)
        logger.info(f"POST_PARSE_DEDUP | flagged={flagged}")
    except Exception as e:
        logger.error(f"POST_PARSE_DEDUP_ERROR | {e}")



def start_scheduler(
    pool: asyncpg.Pool,
    client: TelegramClient,
    bot=None,
) -> AsyncIOScheduler:
    cfg = config['scheduler']
    admin_id = config['telegram']['admin_user_id']

    scheduler = AsyncIOScheduler(timezone='UTC')

    scheduler.add_job(
        job_parse_channels,
        trigger='interval',
        minutes=cfg['parse_interval_minutes'],
        args=[client, pool, bot, bot is not None],
        id='parse_channels',
        name='Parse channels',
        max_instances=1,
        misfire_grace_time=60,
    )

    scheduler.start()
    logger.info(
        f"Scheduler started | "
        f"parse_interval={cfg['parse_interval_minutes']}min | "
        f"cleanup={cfg['cleanup_interval_hours']}h"
    )
    return scheduler