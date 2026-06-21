from loguru import logger
from pathlib import Path
import yaml
from db.tables import Tables

from cinema_collector.scrapers.cineplexx import CineplexxScraper
from cinema_collector.saver import save_events
from cinema_collector.scrapers.cinestar import CineStarScraper
from cinema_collector.scrapers.mts_dvorana import MtsDvoranaScraper
from cinema_collector.scrapers.roda import RodaScraper
from cinema_collector.scrapers.tuckwood import TuckwoodScraper
from cinema_collector.scrapers.cinegrand import CineGrandScraper

def load_cinemas() -> dict:
    root = Path(__file__).parent.parent
    path = root / 'cinemas.yaml'
    with open(path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f).get('cinemas', {})


async def _sync_descriptions(pool, cinemas: dict) -> None:
    cineplexx_description = cinemas.get('cineplexx_description', '') or ''
    for cinema in cinemas.get('cineplexx', []):
        await pool.execute(f"""
            INSERT INTO {Tables.CINEMA_DESCRIPTIONS} (cinema_name, description)
            VALUES ($1, $2)
            ON CONFLICT (cinema_name) DO UPDATE SET description = $2
        """, cinema['name'], cineplexx_description)

    skip_keys = {'cineplexx', 'cineplexx_description'}
    for group, items in cinemas.items():
        if group in skip_keys:
            continue
        if not isinstance(items, list):
            continue
        for cinema in items:
            description = cinema.get('description', '') or ''
            await pool.execute(f"""
                    INSERT INTO {Tables.CINEMA_DESCRIPTIONS} (cinema_name, description)
                    VALUES ($1, $2)
                    ON CONFLICT (cinema_name) DO UPDATE SET description = $2
                """, cinema['name'], description)

    logger.info("CINEMA_DESCRIPTIONS_SYNCED")

async def run_all_scrapers(pool, bot=None) -> None:
    logger.info("CINEMA_RUN_START")
    cinemas = load_cinemas()
    total = 0

    try:
        scraper = CineplexxScraper(cinemas.get('cineplexx', []))
        events = await _run_sync(scraper.fetch)
        saved = await save_events(pool, events, bot=bot)
        total += saved
    except Exception as e:
        logger.error(f"CINEPLEXX_RUNNER_ERROR | {e}")

    try:
        for cinema in cinemas.get('tuckwood', []):
            scraper = TuckwoodScraper(cinema)
            events = await _run_sync(scraper.fetch)
            saved = await save_events(pool, events, bot=bot)
            total += saved
    except Exception as e:
        logger.error(f"TUCKWOOD_RUNNER_ERROR | {e}")

    try:
        for cinema in cinemas.get('cinegrand', []):
            scraper = CineGrandScraper(cinema)
            events = await _run_sync(scraper.fetch)
            saved = await save_events(pool, events, bot=bot)
            total += saved
    except Exception as e:
        logger.error(f"CINEGRAND_RUNNER_ERROR | {e}")

    try:
        for cinema in cinemas.get('cinestar', []):
            scraper = CineStarScraper(cinema)
            events = await _run_sync(scraper.fetch)
            saved = await save_events(pool, events, bot=bot)
            total += saved
    except Exception as e:
            logger.error(f"CINESTAR_RUNNER_ERROR | {e}")

    try:
        for cinema in cinemas.get('roda', []):
            scraper = RodaScraper(cinema)
            events = await _run_sync(scraper.fetch)
            saved = await save_events(pool, events, bot=bot)
            total += saved
    except Exception as e:
        logger.error(f"RODA_RUNNER_ERROR | {e}")

    try:
        for cinema in cinemas.get('mts_dvorana', []):
            scraper = MtsDvoranaScraper(cinema)
            events = await _run_sync(scraper.fetch)
            saved = await save_events(pool, events, bot=bot)
            total += saved
    except Exception as e:
        logger.error(f"MTS_DVORANA_RUNNER_ERROR | {e}")

    try:
        await _sync_descriptions(pool, cinemas)
    except Exception as e:
        logger.error(f"DESCRIPTIONS_SYNC_ERROR | {e}")

    logger.info(f"CINEMA_RUN_DONE | total_saved={total}")


async def _run_sync(func, *args):
    import asyncio
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, func, *args)
