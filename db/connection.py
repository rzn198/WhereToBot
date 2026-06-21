import asyncpg
from loguru import logger
from shared.config import config


async def init_db_pool() -> asyncpg.Pool:
    db = config['db']

    logger.info(f"Connecting to DB: {db['host']}:{db['port']}/{db['name']}")

    try:
        pool = await asyncpg.create_pool(
            host=db['host'],
            port=db['port'],
            database=db['name'],
            user=db['user'],
            password=db['password'],
            min_size=2,
            max_size=10,
            command_timeout=30,
        )
    except Exception as e:
        logger.error(f"DB connection failed: {type(e).__name__}: {e}")
        raise

    logger.info("DB pool initialized")
    return pool


async def close_db_pool(pool: asyncpg.Pool) -> None:
    await pool.close()
    logger.info("DB pool closed")