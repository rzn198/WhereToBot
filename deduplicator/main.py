"""Сервис дедупликатора: раз в poll_seconds смотрит, закончили ли коллекторы, и запускает прогон run_dedup."""
import asyncio
import os
import sys

if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

os.environ.setdefault('APP_ENV', 'production')

from loguru import logger

from deduplicator import notify
from deduplicator.runner import run_dedup
from deduplicator.schedule import ServiceSettings, close_interrupted_runs, decide, load_snapshot, service_settings
from shared.ai import client_from_config


async def tick(pool, client, cfg: dict, st: ServiceSettings, *, now=None, send=None):
    """Один проход: решаем, пора ли запускаться; если да — прогон и сообщения админу. Возвращает RunResult или None."""
    method = cfg.get('method', 'legacy')
    if method not in ('shadow', 'ai'):
        return None
    snap = await load_snapshot(pool, st, now)
    go, why = decide(snap, st)
    if not go:
        logger.debug(f"DEDUP_SERVICE_WAIT | {why}")
        return None
    logger.info(f"DEDUP_SERVICE_RUN | method={method} | reason={why}")
    res = await run_dedup(pool, client, cfg, method=method)
    st_ = res.stats
    if res.status == 'ok':
        logger.info(f"DEDUP_SERVICE_DONE | status=ok | dates={st_.get('dates_processed')}/{st_.get('dates_total')} "
                    f"| requests={st_.get('requests')} | groups={st_.get('groups_found')} | hidden={st_.get('hidden')} "
                    f"| unhidden={st_.get('unhidden')} | bad_days={st_.get('bad_days')} | skipped_days={st_.get('skipped_days')}")
    else:
        logger.warning(f"DEDUP_SERVICE_DONE | status={res.status} | {res.error}")
    if st.alerts:
        send = send or notify.send_admin
        texts = [notify.transition_text(snap.last_run_status, res.status, res.error, st)]
        if res.status == 'ok':
            texts.append(notify.run_report(res, method))
        for t in texts:
            if t:
                await send(t)
    return res


async def main():
    from db.connection import close_db_pool, init_db_pool
    from db.validators import validate_config_tables
    from shared.config import config
    from shared.logging import setup_logging

    setup_logging('deduplicator')
    cfg = config['deduplication']
    st = service_settings(cfg)
    logger.info(f"Deduplicator starting | env={config['env']} | method={cfg['method']}")
    pool = await init_db_pool()
    await validate_config_tables(pool)
    closed = await close_interrupted_runs(pool)
    if closed:
        logger.warning(f"DEDUP_INTERRUPTED_RUNS_CLOSED | rows={closed}")
    if cfg['method'] not in ('shadow', 'ai'):
        logger.warning("DEDUP_SERVICE_IDLE | deduplication.method=legacy: сервис простаивает (включи shadow или ai и перезапусти)")
    client = client_from_config('dedup', cfg['ai'])
    try:
        while True:
            try:
                await tick(pool, client, cfg, st)
            except Exception as e:
                logger.exception(f"DEDUP_SERVICE_TICK_ERROR | {type(e).__name__}: {e}")
            await asyncio.sleep(st.poll_seconds)
    finally:
        await close_db_pool(pool)
        logger.info("Deduplicator stopped")


if __name__ == '__main__':
    asyncio.run(main())
