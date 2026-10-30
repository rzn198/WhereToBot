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

    Оптимизация «только новые»: один раз читаем все source_url, уже
    сохранённые в БД, и передаём это множество каждому скраперу как
    known_urls. Адаптированные скраперы пропускают известные события
    ДО дорогих detail-запросов (экономия сети/времени). Неадаптированные
    скраперы параметр игнорируют и работают как раньше.
    """
    logger.info("EVENT_RUN_START")

    # известные source_url — один запрос на весь прогон
    try:
        rows = await pool.fetch("SELECT source_url FROM events")
        known = {r["source_url"] for r in rows}
    except Exception as e:
        logger.error(f"EVENT_KNOWN_ERROR | {e}")
        known = set()
    logger.info(f"EVENT_KNOWN | count={len(known)}")

    total = 0

    for scraper_cls in SCRAPERS:
        name = getattr(scraper_cls, 'source', scraper_cls.__name__)
        try:
            scraper = scraper_cls()
            items = await _call_fetch(scraper, known)
            saved = await save_events(pool, items)
            total += saved
        except Exception as e:
            logger.error(f"EVENT_SCRAPER_ERROR | source={name} | {e}")

    # Шаг перевода: новые события (description_ru IS NULL) -> русский.
    # Изолирован try/except: сбой перевода не роняет коллектор.
    try:
        from event_collector.translator import translate_new
        await translate_new(pool)
    except Exception as e:
        logger.error(f"EVENT_TRANSLATE_ERROR | {e}")

    logger.info(f"EVENT_RUN_DONE | total_saved={total}")
    return total


async def _call_fetch(scraper, known):
    """
    Вызывает scraper.fetch, передавая known_urls если скрапер его принимает.
    Обратная совместимость: старые скраперы с fetch() без параметра
    вызываются как раньше.
    """
    import inspect
    sig = inspect.signature(scraper.fetch)
    if "known_urls" in sig.parameters:
        return await scraper.fetch(known_urls=known)
    return await scraper.fetch()
