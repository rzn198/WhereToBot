import sys
from pathlib import Path
from loguru import logger

ROOT = Path(__file__).parent.parent
LOGS_DIR = ROOT / 'logs'


def _inject_update_id(record):
    try:
        from bot.middleware.correlation import get_current_update_id
        record["extra"]["update_id"] = get_current_update_id()
    except ImportError:
        record["extra"]["update_id"] = 0


def setup_logging(service: str = 'app') -> None:

    LOGS_DIR.mkdir(exist_ok=True)

    logger.remove()
    logger.configure(patcher=_inject_update_id)

    logger.add(
        sys.stdout,
        format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | <cyan>{name}</cyan> | <yellow>upd:{extra[update_id]}</yellow> | {message}",
        level="DEBUG",
        colorize=True,
    )

    logger.add(
        LOGS_DIR / f"{service}.log",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {name} | upd:{extra[update_id]} | {message}",
        level="INFO",
        rotation="50 MB",
        retention="7 days",
        compression="zip",
        encoding="utf-8",
    )

    logger.add(
        LOGS_DIR / f"{service}_errors.log",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {name} | upd:{extra[update_id]} | {message}",
        level="WARNING",
        rotation="20 MB",
        retention="30 days",
        compression="zip",
        encoding="utf-8",
    )

    logger.info(f"Logging initialized | service={service}")
