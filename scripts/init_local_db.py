import asyncio
import sys
import os
from pathlib import Path

if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

os.environ['APP_ENV'] = 'local'

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from shared.logging import setup_logging
from loguru import logger


async def init():
    setup_logging('init_db')

    import asyncpg
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env.local')

    host     = os.getenv('DB_HOST', 'localhost')
    port     = int(os.getenv('DB_PORT', 5432))
    user     = os.getenv('DB_USER', 'postgres')
    password = os.getenv('DB_PASSWORD', '')
    db_name  = os.getenv('DB_NAME', 'eventbot_local')

    logger.info(f"Connecting to PostgreSQL: {host}:{port} as {user}")

    try:
        sys_conn = await asyncpg.connect(
            host=host, port=port,
            user=user, password=password,
            database='postgres'
        )
    except Exception as e:
        logger.error(f"Cannot connect to PostgreSQL: {e}")
        logger.error("Убедись что PostgreSQL запущен и пароль в .env.local верный")
        sys.exit(1)

    # Создаём БД если не существует
    exists = await sys_conn.fetchval(
        "SELECT 1 FROM pg_database WHERE datname = $1", db_name
    )

    if exists:
        logger.info(f"DB '{db_name}' already exists — skipping creation")
    else:
        await sys_conn.execute(f'CREATE DATABASE "{db_name}"')
        logger.info(f"DB '{db_name}' created")

    await sys_conn.close()

    conn = await asyncpg.connect(
        host=host, port=port,
        user=user, password=password,
        database=db_name
    )

    schema_path = ROOT / 'schema.sql'
    if not schema_path.exists():
        logger.error(f"schema.sql не найден: {schema_path}")
        sys.exit(1)

    schema = schema_path.read_text(encoding='utf-8')

    try:
        await conn.execute(schema)
        logger.info("schema.sql applied successfully")
    except asyncpg.PostgresError as e:
        if 'already exists' in str(e):
            logger.info("Tables already exist — schema is up to date")
        else:
            logger.error(f"Schema error: {e}")
            sys.exit(1)
    finally:
        await conn.close()

    logger.info(f"Done. Local DB ready: {db_name} on {host}:{port}")


if __name__ == '__main__':
    asyncio.run(init())