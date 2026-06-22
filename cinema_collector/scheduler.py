from apscheduler.schedulers.asyncio import AsyncIOScheduler
from loguru import logger

from shared.config import config
from cinema_collector.runner import run_all_scrapers


async def job_parse_cinema(pool, bot=None) -> None:
    await run_all_scrapers(pool, bot=bot)


def start_cinema_scheduler(pool, bot=None) -> AsyncIOScheduler:
    cfg = config.get('cinema_collector', {})
    interval_hours = cfg.get('parse_interval_hours', 6)

    scheduler = AsyncIOScheduler(timezone='UTC')

    scheduler.add_job(
        job_parse_cinema,
        trigger='interval',
        hours=interval_hours,
        args=[pool, bot],
        id='parse_cinema',
        name='Parse cinema schedule',
        max_instances=1,
    )

    scheduler.start()
    logger.info(f"Cinema scheduler started | interval={interval_hours}h")
    return scheduler
