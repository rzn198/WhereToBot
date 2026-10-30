# event_collector/main.py
# Точка входа event_collector контейнера.

import asyncio
import os
import sys

if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

os.environ.setdefault('APP_ENV', 'production')

from loguru import logger
from shared.logging import setup_logging
from shared.config import config
from db.connection import init_db_pool, close_db_pool
from db.validators import validate_config_tables
from event_collector.scheduler import start_event_scheduler
from event_collector.runner import run_all_scrapers


async def main():
    setup_logging('event_collector')
    logger.info(f"Event collector starting | env={config['env']}")

    pool = await init_db_pool()
    await validate_config_tables(pool)

    # Первый запуск сразу при старте
    logger.info("Running initial event scrape...")
    await run_all_scrapers(pool)

    # Планировщик
    scheduler = start_event_scheduler(pool)

    try:
        logger.info("Event collector started")
        await asyncio.Event().wait()
    finally:
        logger.info("Event collector stopping...")
        scheduler.shutdown()
        await close_db_pool(pool)
        logger.info("Event collector stopped")


if __name__ == '__main__':
    asyncio.run(main())
