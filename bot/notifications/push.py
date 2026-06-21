import asyncio
from datetime import date
from loguru import logger
from telegram import Bot, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.error import TelegramError, RetryAfter, Forbidden
from shared.config import config
from db.tables import Tables

SEND_DELAY = config['alerts']['send_delay_seconds']      # ~0.04 сек = 25 сообщ/сек


async def send_push_notifications(bot: Bot, pool) -> None:
    today = date.today()
    logger.info(f"PUSH_START | date={today}")

    rows = await pool.fetch(
        f"""
        SELECT
            f.user_id,
            array_agg(m.text ORDER BY m.date, m.created_at) as event_texts
        FROM {Tables.FAVORITES} f
        JOIN {Tables.MESSAGES} m ON m.surrogate_id = f.msg_id
        WHERE m.date = $1
        AND m.surrogate_id NOT IN (
            SELECT msg_id FROM {Tables.HIDDEN_EVENTS}
            WHERE user_id = f.user_id
        )
        GROUP BY f.user_id
        """,
        today
    )

    if not rows:
        logger.info("PUSH_SKIP | no favorites for today")
        return

    logger.info(f"PUSH_USERS | count={len(rows)}")

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton('Да, вперёд! ✅', callback_data='alert:dismiss')
    ]])

    success = 0
    failed = 0

    for row in rows:
        user_id = row['user_id']
        events = row['event_texts']

        alert_msg = config['alerts']['message'] + "\n\n"
        for i, event_text in enumerate(events, 1):
            preview = event_text[:200] + ('...' if len(event_text) > 200 else '')
            alert_msg += f"{i}. {preview}\n\n"

        try:
            await bot.send_message(
                chat_id=user_id,
                text=alert_msg,
                reply_markup=keyboard,
            )
            success += 1
            logger.info(f"PUSH_SENT | user_id={user_id} | events={len(events)}")

        except RetryAfter as e:
            logger.warning(f"PUSH_RETRY | user_id={user_id} | wait={e.retry_after}s")
            await asyncio.sleep(e.retry_after + 1)
            try:
                await bot.send_message(
                    chat_id=user_id,
                    text=alert_msg,
                    reply_markup=keyboard,
                )
                success += 1
            except TelegramError as e2:
                logger.error(f"PUSH_FAILED_RETRY | user_id={user_id} | {e2}")
                failed += 1

        except Forbidden:
            logger.warning(f"PUSH_BLOCKED | user_id={user_id}")
            failed += 1

        except TelegramError as e:
            logger.error(f"PUSH_ERROR | user_id={user_id} | {e}")
            failed += 1

        await asyncio.sleep(SEND_DELAY)

    logger.info(
        f"PUSH_DONE | success={success} failed={failed} total={len(rows)}"
    )
