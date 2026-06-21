import sys
from pathlib import Path
from loguru import logger

ROOT = Path(__file__).parent.parent
LOGS_DIR = ROOT / 'logs'


def setup_logging(service: str = 'app') -> None:

    LOGS_DIR.mkdir(exist_ok=True)

    logger.remove()

    logger.add(
        sys.stdout,
        format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | <cyan>{name}</cyan> | {message}",
        level="DEBUG",
        colorize=True,
    )

    logger.add(
        LOGS_DIR / f"{service}.log",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {name} | {message}",
        level="INFO",
        rotation="50 MB",
        retention="7 days",
        compression="zip",
        encoding="utf-8",
    )

    logger.add(
        LOGS_DIR / f"{service}_errors.log",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {name} | {message}",
        level="WARNING",
        rotation="20 MB",
        retention="30 days",
        compression="zip",
        encoding="utf-8",
    )

    logger.info(f"Logging initialized | service={service}")