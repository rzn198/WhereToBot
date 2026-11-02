import asyncio
import time
from concurrent.futures import ThreadPoolExecutor

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
from cinema_collector import health
from cinema_collector.health import RunResult

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


# (источник, как собрать скраперы из cinemas.yaml). Пустой список — источник не настроен, пропускаем.
SOURCES = [
    ('cineplexx',   lambda c: [CineplexxScraper(c['cineplexx'])] if c.get('cineplexx') else []),
    ('tuckwood',    lambda c: [TuckwoodScraper(x) for x in c.get('tuckwood', [])]),
    ('cinegrand',   lambda c: [CineGrandScraper(x) for x in c.get('cinegrand', [])]),
    ('cinestar',    lambda c: [CineStarScraper(x) for x in c.get('cinestar', [])]),
    ('roda',        lambda c: [RodaScraper(x) for x in c.get('roda', [])]),
    ('mts_dvorana', lambda c: [MtsDvoranaScraper(x) for x in c.get('mts_dvorana', [])]),
]

SAVE_CONCURRENCY = 2


def _fetch_source(source: str, scrapers: list) -> tuple[list, str | None, float]:
    """Синхронно (в потоке) опросить все скраперы источника. Сбой одного не останавливает остальные."""
    t0 = time.perf_counter()
    events: list = []
    error: str | None = None
    for scraper in scrapers:
        try:
            events.extend(scraper.fetch())
        except Exception as e:
            logger.error(f"{source.upper()}_RUNNER_ERROR | {type(e).__name__}: {e}")
            error = error or f"{type(e).__name__}: {e}"
    return events, error, time.perf_counter() - t0


async def _previous_counts(pool) -> dict[str, int]:
    try:
        rows = await pool.fetch(f"SELECT source, count(*) AS n FROM {Tables.CINEMA_EVENTS} GROUP BY source")
        return {r['source']: int(r['n']) for r in rows}
    except Exception as e:
        logger.warning(f"CINEMA_PREVIOUS_COUNTS_ERROR | {e}")
        return {}


async def _process_source(pool, bot, source: str, scrapers: list, executor,
                          previous: dict, save_sem: asyncio.Semaphore) -> RunResult:
    loop = asyncio.get_running_loop()
    events, error, seconds = await loop.run_in_executor(executor, _fetch_source, source, scrapers)

    saved = 0
    if events:
        try:
            async with save_sem:
                saved = await save_events(pool, events, bot=bot)
        except Exception as e:
            logger.error(f"{source.upper()}_SAVE_ERROR | {type(e).__name__}: {e}")
            error = error or f"сохранение: {type(e).__name__}: {e}"

    newest = max((e.showtime for e in events), default=None)
    return RunResult(
        source=source, events=len(events), saved=saved,
        movies=len({e.movie_id or e.movie_title for e in events}),
        seconds=seconds, error=error, previous=previous.get(source), newest=newest,
    )


async def run_all_scrapers(pool, bot=None) -> None:
    """Источники опрашиваются параллельно; результат каждого уходит в health-монитор."""
    logger.info("CINEMA_RUN_START")
    t0 = time.perf_counter()
    cinemas = load_cinemas()
    previous = await _previous_counts(pool)

    jobs = [(name, make(cinemas)) for name, make in SOURCES]
    jobs = [(name, scrapers) for name, scrapers in jobs if scrapers]

    save_sem = asyncio.Semaphore(SAVE_CONCURRENCY)
    with ThreadPoolExecutor(max_workers=max(len(jobs), 1), thread_name_prefix='cinema') as executor:
        results: list[RunResult] = await asyncio.gather(*[
            _process_source(pool, bot, name, scrapers, executor, previous, save_sem)
            for name, scrapers in jobs
        ])

    await health.report(list(results))

    try:
        await _sync_descriptions(pool, cinemas)
    except Exception as e:
        logger.error(f"DESCRIPTIONS_SYNC_ERROR | {e}")

    total = sum(r.saved for r in results)
    logger.info(f"CINEMA_RUN_DONE | total_saved={total} | seconds={time.perf_counter() - t0:.1f}")


async def _run_sync(func, *args):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, func, *args)
