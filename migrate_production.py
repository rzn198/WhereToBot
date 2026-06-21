import asyncio
import os
os.environ.setdefault('APP_ENV', 'production')

from db.connection import init_db_pool, close_db_pool
from loguru import logger


async def main():
    pool = await init_db_pool()

    logger.info("Migrating cinema_events...")

    await pool.execute("""
        ALTER TABLE cinema_events
        ADD COLUMN IF NOT EXISTS movie_id TEXT DEFAULT ''
    """)

    await pool.execute("""
        ALTER TABLE cinema_events
        ADD COLUMN IF NOT EXISTS movie_title_serbian TEXT DEFAULT ''
    """)

    await pool.execute("""
        ALTER TABLE cinema_events
        ADD COLUMN IF NOT EXISTS movie_title_normalized TEXT DEFAULT ''
    """)

    await pool.execute("""
        ALTER TABLE cinema_events
        ADD COLUMN IF NOT EXISTS movie_description_short TEXT DEFAULT ''
    """)

    await pool.execute("""
        ALTER TABLE cinema_events
        ADD COLUMN IF NOT EXISTS movie_description_full TEXT DEFAULT ''
    """)

    logger.info("cinema_events migrated ✅")

    # 2. user_stats — добавляем menu_message_id и menu_message_type
    logger.info("Migrating user_stats...")

    await pool.execute("""
        ALTER TABLE user_stats
        ADD COLUMN IF NOT EXISTS menu_message_id BIGINT DEFAULT NULL
    """)

    await pool.execute("""
        ALTER TABLE user_stats
        ADD COLUMN IF NOT EXISTS menu_message_type VARCHAR(10) DEFAULT 'text'
    """)

    logger.info("user_stats migrated ✅")

    # 3. cinema_descriptions — создаём таблицу
    logger.info("Creating cinema_descriptions...")

    await pool.execute("""
        CREATE TABLE IF NOT EXISTS cinema_descriptions (
            cinema_name TEXT PRIMARY KEY,
            description TEXT NOT NULL DEFAULT ''
        )
    """)

    logger.info("cinema_descriptions created ✅")

    # 4. Проверяем результат
    logger.info("Verifying migrations...")

    for table in ['cinema_events', 'user_stats', 'cinema_descriptions']:
        rows = await pool.fetch("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = $1
            ORDER BY ordinal_position
        """, table)
        columns = [r['column_name'] for r in rows]
        logger.info(f"{table}: {columns}")

    await close_db_pool(pool)
    logger.info("Migration completed ✅")


asyncio.run(main())
