import asyncio
import time

from loguru import logger

from datetime import date, timedelta

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

    logger.info("EVENT_RUN_START")

    try:
        rows = await pool.fetch("SELECT source_url, starts_at FROM events")
        known = {r["source_url"]: r["starts_at"] for r in rows}
    except Exception as e:
        logger.error(f"EVENT_KNOWN_ERROR | {e}")
        known = {}
    logger.info(f"EVENT_KNOWN | count={len(known)}")

    known = _drop_upcoming_for_refresh(known)

    started = time.perf_counter()
    results = await asyncio.gather(
        *(_run_one(cls, pool, known) for cls in SCRAPERS)
    )
    total = sum(r.saved for r in results)
    logger.info(
        f"EVENT_SCRAPERS_DONE | seconds={time.perf_counter() - started:.1f}"
    )

    await health.report(list(results))

    try:
        from event_collector.translator import translate_new
        await translate_new(pool)
    except Exception as e:
        logger.error(f"EVENT_TRANSLATE_ERROR | {e}")

    logger.info(f"EVENT_RUN_DONE | total_saved={total}")
    return total


_last_refresh_day: date | None = None


def _refresh_days() -> int:
    from shared.config import config
    cfg = config.get('event_collector', {}) or {}
    try:
        return max(0, int(cfg.get('refresh_upcoming_days', 7)))
    except (TypeError, ValueError):
        return 7


def _drop_upcoming_for_refresh(known: dict, today: date | None = None) -> dict:
    global _last_refresh_day
    days = _refresh_days()
    today = today or date.today()
    if days <= 0 or _last_refresh_day == today:
        return known
    _last_refresh_day = today
    horizon = today + timedelta(days=days)
    keep = {u: d for u, d in known.items()
            if not (d is not None and d <= horizon)}
    logger.info(
        f"EVENT_REFRESH | upcoming_days={days} | refetch={len(known) - len(keep)}"
    )
    return keep


async def _run_one(scraper_cls, pool, known) -> RunResult:
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
    reports = scraper is not None and hasattr(scraper, 'listed_count')
    return RunResult(
        source=name, saved=saved, seconds=seconds, error=error,
        reports=reports,
        listed=getattr(scraper, 'listed_count', None) if reports else None,
    )


async def _call_fetch(scraper, known):
    import inspect
    sig = inspect.signature(scraper.fetch)
    if "known_urls" in sig.parameters:
        return await scraper.fetch(known_urls=known)
    return await scraper.fetch()
