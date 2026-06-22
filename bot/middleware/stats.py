import asyncpg
from loguru import logger
from telegram import Update

from db.tables import Tables


async def update_user_stats(
    pool: asyncpg.Pool,
    update: Update,
) -> None:
    user = update.effective_user
    if not user:
        return

    try:
        await pool.execute(f"""
            INSERT INTO {Tables.USER_STATS} (user_id, first_seen, last_seen, actions_count)
            VALUES ($1, NOW(), NOW(), 1)
            ON CONFLICT (user_id) DO UPDATE SET
                last_seen     = NOW(),
                actions_count = {Tables.USER_STATS}.actions_count + 1
        """, user.id)
    except Exception as e:
        logger.error(f"Failed to update user_stats | user_id={user.id} | {e}")