from pathlib import Path
from loguru import logger
from telethon import TelegramClient

from shared.config import config

SESSION_DIR = Path(__file__).parent.parent / 'session'
SESSION_FILE = SESSION_DIR / 'collector'


def build_telethon_client() -> TelegramClient:

    SESSION_DIR.mkdir(exist_ok=True)

    tg = config['telegram']

    client = TelegramClient(
        session=str(SESSION_FILE),
        api_id=tg['api_id'],
        api_hash=tg['api_hash'],
    )

    logger.info(f"Telethon client created | session={SESSION_FILE}")
    return client