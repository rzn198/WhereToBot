# event_collector/runner.py
# Запуск всех event-скраперов по очереди и сохранение в БД.
# Скраперы async (httpx) — вызываются напрямую, без executor.
# Каждый скрапер изолирован try/except: падение одного не роняет остальные.

import asyncio
import time

from loguru import logger

from event_collector import health
from event_collector.health import RunResult
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

    # Скраперы ходят на разные сайты и независимы друг от друга, поэтому
    # идут одновременно: общее время ~ самый долгий скрапер, а не сумма.
    started = time.perf_counter()
    results = await asyncio.gather(
        *(_run_one(cls, pool, known) for cls in SCRAPERS)
    )
    total = sum(r.saved for r in results)
    logger.info(
        f"EVENT_SCRAPERS_DONE | seconds={time.perf_counter() - started:.1f}"
    )

    # Мониторинг: сломанный источник -> алерт админу (ошибки гасятся внутри)
    await health.report(list(results))

    # Шаг перевода: новые события (description_ru IS NULL) -> русский.
    # Изолирован try/except: сбой перевода не роняет коллектор.
    try:
        from event_collector.translator import translate_new
        await translate_new(pool)
    except Exception as e:
        logger.error(f"EVENT_TRANSLATE_ERROR | {e}")

    logger.info(f"EVENT_RUN_DONE | total_saved={total}")
    return total


async def _run_one(scraper_cls, pool, known) -> RunResult:
    """Один скрапер: fetch + сохранение. Сбой изолирован, наружу уходит
    RunResult (для логов и мониторинга)."""
    name = getattr(scraper_cls, 'source', scraper_cls.__name__)
    t0 = time.perf_counter()
    scraper = None
    try:
        scraper = scraper_cls()
        items = await _call_fetch(scraper, known)
        saved = await save_events(pool, items)
        seconds = time.perf_counter() - t0
        logger.info(
            f"EVENT_SCRAPER_DONE | source={name} | items={len(items)} | "
            f"saved={saved} | seconds={seconds:.1f}"
        )
        return _result(name, scraper, saved, seconds)
    except Exception as e:
        logger.error(f"EVENT_SCRAPER_ERROR | source={name} | {e}")
        return _result(name, scraper, 0, time.perf_counter() - t0, error=str(e))


def _result(name, scraper, saved, seconds, error=None) -> RunResult:
    # у скрапера есть атрибут listed_count -> он сообщает размер листинга
    # (None = до листинга не дошёл); нет атрибута -> не оцениваем.
    reports = scraper is not None and hasattr(scraper, 'listed_count')
    return RunResult(
        source=name, saved=saved, seconds=seconds, error=error,
        reports=reports,
        listed=getattr(scraper, 'listed_count', None) if reports else None,
    )


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
