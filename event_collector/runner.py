# event_collector/runner.py
# Запуск всех event-скраперов по очереди и сохранение в БД.
# Скраперы async (httpx) — вызываются напрямую, без executor.
# Каждый скрапер изолирован try/except: падение одного не роняет остальные.

from loguru import logger

from event_collector.saver import save_events
from event_collector.scrapers.balkan_promo import BalkanPromoScraper
from event_collector.scrapers.biletrs import BiletRsScraper
from event_collector.scrapers.efinityrs import EfinityRsScraper
from event_collector.scrapers.gigstix import GigstixScraper
from event_collector.scrapers.polako import PolakoScraper
from event_collector.scrapers.ticketsrs import TicketsRsScraper
from event_collector.scrapers.ticrs import TicRsScraper

# Список активных скраперов. Добавление нового источника =
# добавить класс сюда (и файл в scrapers/).
SCRAPERS = [
    EfinityRsScraper,
    PolakoScraper,
    BiletRsScraper,
    TicketsRsScraper,
    TicRsScraper,
    BalkanPromoScraper,
    GigstixScraper,
]


async def run_all_scrapers(pool) -> int:
    """
    Прогоняет все скраперы по очереди, сохраняет события в БД.
    Возвращает суммарное количество сохранённых записей.
    """
    logger.info("EVENT_RUN_START")
    total = 0

    for scraper_cls in SCRAPERS:
        name = getattr(scraper_cls, 'source', scraper_cls.__name__)
        try:
            scraper = scraper_cls()
            items = await scraper.fetch()
            saved = await save_events(pool, items)
            total += saved
        except Exception as e:
            logger.error(f"EVENT_SCRAPER_ERROR | source={name} | {e}")

    logger.info(f"EVENT_RUN_DONE | total_saved={total}")
    return total
