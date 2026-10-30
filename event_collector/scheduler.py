# event_collector/scheduler.py
# Планировщик запуска event-скраперов по интервалу.

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from loguru import logger

from shared.config import config
from event_collector.runner import run_all_scrapers


async def job_parse_events(pool) -> None:
    """Прогоняет все event-скраперы."""
    await run_all_scrapers(pool)


def start_event_scheduler(pool) -> AsyncIOScheduler:
    cfg = config.get('event_collector', {})
    interval_hours = cfg.get('parse_interval_hours', 6)

    scheduler = AsyncIOScheduler(timezone='UTC')

    scheduler.add_job(
        job_parse_events,
        trigger='interval',
        hours=interval_hours,
        args=[pool],
        id='parse_events',
        name='Parse regional events',
        max_instances=1,
    )

    scheduler.start()
    logger.info(f"Event scheduler started | interval={interval_hours}h")
    return scheduler
