import asyncpg
from loguru import logger
from db.tables import Tables


async def validate_config_tables(pool: asyncpg.Pool) -> None:
    expected = {
        getattr(Tables, attr)
        for attr in dir(Tables)
        if not attr.startswith('_')
    }

    existing = {
        row['tablename']
        for row in await pool.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        )
    }

    missing = expected - existing

    if missing:
        raise RuntimeError(
            f"Таблицы из Tables не найдены в БД: {missing}. "
            f"Запусти scripts/init_local_db.py"
        )

    logger.info(f"DB validation passed | tables={sorted(expected)}")