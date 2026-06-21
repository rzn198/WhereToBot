from apscheduler.schedulers.asyncio import AsyncIOScheduler
from loguru import logger
import shutil
from shared.config import config
from bot.notifications.push import send_push_notifications
from bot.alerts import send_scheduled_alerts
from bot.middleware.locks import cleanup_stale_locks


async def job_cleanup(pool) -> None:
    from db.cleanup import run_cleanup
    await run_cleanup(pool)


async def job_check_disk(bot) -> None:
    cfg = config.get('monitoring', {})
    threshold = cfg.get('disk_free_threshold_pct', 10)
    admin_id = config['telegram']['admin_user_id']

    total, used, free = shutil.disk_usage('/')
    free_pct = free / total * 100

    if free_pct < threshold:
        msg = f"⚠️ Мало места на диске: {free_pct:.1f}% свободно"
        logger.critical(f"Low disk space: {free_pct:.1f}% free")
        try:
            await bot.send_message(admin_id, msg)
        except Exception as e:
            logger.error(f"Failed to send disk alert: {e}")
    else:
        logger.debug(f"Disk OK: {free_pct:.1f}% free")


def start_bot_scheduler(bot, pool) -> AsyncIOScheduler:
    cfg = config['scheduler']
    alert_time = cfg['push_alert_time']
    hour, minute = map(int, alert_time.split(':'))

    scheduler = AsyncIOScheduler(timezone='UTC')

    scheduler.add_job(
        send_push_notifications,
        trigger='cron',
        hour=hour,
        minute=minute,
        args=[bot, pool],
        id='push_notifications',
        name='Push notifications',
        max_instances=1,
    )

    scheduler.add_job(
        job_cleanup,
        trigger='interval',
        hours=cfg['cleanup_interval_hours'],
        args=[pool],
        id='cleanup',
        name='Cleanup old events',
        max_instances=1,
    )

    scheduler.add_job(
        job_check_disk,
        trigger='interval',
        hours=cfg['disk_check_interval_hours'],
        args=[bot],
        id='disk_check',
        name='Check disk space',
        max_instances=1,
    )

    scheduler.add_job(
        send_scheduled_alerts,
        trigger='interval',
        minutes=1,
        args=[bot, pool, scheduler],
        id='scheduled_alerts',
        name='Scheduled alerts',
        max_instances=1,
    )

    scheduler.add_job(
        cleanup_stale_locks,
        trigger='interval',
        hours=1,
        id='cleanup_stale_locks',
        name='Cleanup inactive user locks',
        max_instances=1,
    )

    scheduler.start()
    logger.info(f"Bot scheduler started | push_time={alert_time} UTC")
    return scheduler
