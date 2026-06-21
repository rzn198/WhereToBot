import asyncio
from datetime import datetime, timezone, timedelta
from loguru import logger
from telegram import Bot, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.error import TelegramError, RetryAfter, Forbidden

from db.tables import Tables
from shared.config import config


SEND_DELAY = 0.04   # секунды между сообщениями
CHUNK_SIZE = 500


async def send_scheduled_alerts(bot: Bot, pool, scheduler=None) -> None:

    alerts = await pool.fetch(f"""
        SELECT id, text FROM {Tables.SCHEDULED_ALERTS}
        WHERE scheduled_at <= NOW()
        AND sent_at IS NULL
        ORDER BY scheduled_at
    """)

    if not alerts:
        return

    alerts_cfg = config.get('alerts', {})
    delete_after_hours = alerts_cfg.get('delete_after_hours')

    for alert in alerts:
        alert_id = alert['id']
        text = alert['text']

        logger.info(f"ALERT_SEND_START | alert_id={alert_id}")

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton('Да, вперёд! ✅', callback_data='alert:dismiss')
        ]])

        sent = 0
        failed = 0
        offset = 0

        while True:
            users = await pool.fetch(f"""
                SELECT user_id FROM {Tables.USER_STATS}
                ORDER BY user_id
                LIMIT $1 OFFSET $2
            """, CHUNK_SIZE, offset)

            if not users:
                break

            for user in users:
                user_id = user['user_id']
                try:
                    msg = await bot.send_message(
                        chat_id=user_id,
                        text=f"📢 {text}",
                        reply_markup=keyboard,
                    )
                    sent += 1

                    if delete_after_hours and scheduler:
                        from bot.handlers.alert_dismiss import delete_alert_message
                        run_date = datetime.now(timezone.utc) + timedelta(hours=delete_after_hours)
                        scheduler.add_job(
                            delete_alert_message,
                            trigger='date',
                            run_date=run_date,
                            args=[bot, user_id, msg.message_id],
                            id=f'alert_delete_{alert_id}_{user_id}',
                            max_instances=1,
                        )

                    await asyncio.sleep(SEND_DELAY)

                except RetryAfter as e:
                    logger.warning(f"ALERT_RETRY | user_id={user_id} | retry_after={e.retry_after}")
                    await asyncio.sleep(e.retry_after + 1)
                    try:
                        msg = await bot.send_message(
                            chat_id=user_id,
                            text=f"📢 {text}",
                            reply_markup=keyboard,
                        )
                        sent += 1
                    except Exception:
                        failed += 1

                except Forbidden:
                    failed += 1

                except TelegramError as e:
                    logger.error(f"ALERT_ERROR | user_id={user_id} | {e}")
                    failed += 1

            offset += CHUNK_SIZE

        await pool.execute(f"""
            UPDATE {Tables.SCHEDULED_ALERTS}
            SET sent_at = NOW()
            WHERE id = $1
        """, alert_id)

        logger.info(
            f"ALERT_SEND_DONE | alert_id={alert_id} | "
            f"sent={sent} | failed={failed}"
        )
