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
from cinema_collector.scheduler import start_cinema_scheduler
from cinema_collector.runner import run_all_scrapers


async def main():
    setup_logging('cinema_collector')
    logger.info(f"Cinema collector starting | env={config['env']}")

    pool = await init_db_pool()
    await validate_config_tables(pool)

    logger.info("Running initial cinema scrape...")
    await run_all_scrapers(pool, bot=None)

    scheduler = start_cinema_scheduler(pool)

    try:
        logger.info("Cinema collector started")
        await asyncio.Event().wait()
    finally:
        logger.info("Cinema collector stopping...")
        scheduler.shutdown()
        await close_db_pool(pool)
        logger.info("Cinema collector stopped")


if __name__ == '__main__':
    asyncio.run(main())
