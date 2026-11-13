"""Сервис дедупликатора: раз в poll_seconds смотрит, закончили ли коллекторы, и запускает прогон run_dedup."""
import asyncio
import os
import sys

if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

os.environ.setdefault('APP_ENV', 'production')

from loguru import logger

from db.review_cleanup import ReviewCleanupJob
from deduplicator import notify
from deduplicator.audit import audit_from_cfg
from deduplicator.lock import try_lock
from deduplicator.runner import run_dedup
from deduplicator.schedule import ServiceSettings, close_interrupted_runs, decide, load_snapshot, service_settings
from shared.ai import client_from_config
from translator.service import TranslationHook, translation_settings


async def tick(pool, client, cfg: dict, st: ServiceSettings, *, now=None, send=None, on_ok=None):
    """Один проход: решаем, пора ли запускаться; если да — прогон и сообщения админу. Возвращает RunResult или None."""
    method = cfg.get('method', 'legacy')
    if method not in ('shadow', 'ai'):
        return None
    snap = await load_snapshot(pool, st, now)
    go, why = decide(snap, st)
    if not go:
        logger.debug(f"DEDUP_SERVICE_WAIT | {why}")
        return None
    async with try_lock(pool) as got:
        if not got:
            logger.info("DEDUP_SERVICE_BUSY | другой прогон дедупликации уже идёт (ручной запуск?) — пропускаю такт")
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
    if res.status == 'ok' and on_ok is not None:
        try:
            on_ok(res)                    # перевод победителей запускается ТОЛЬКО после успешного прогона
        except Exception as e:
            logger.exception(f"DEDUP_SERVICE_ON_OK_ERROR | {type(e).__name__}: {e}")
    return res


async def run_after_jobs(res, hook, reviews_job) -> int | None:
    """Задачи, которые идут ПОСЛЕ остальных: чистка пересмотров — только когда в этом такте не было прогона
    дедупликации (res is None) и перевод не работает. Если прогон или перевод идут, подождёт следующего такта."""
    if res is not None or hook.running:
        return None
    return await reviews_job.run_if_due()


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
    aud = audit_from_cfg(cfg)
    logger.info(f"Dedup audit log | enabled={aud.enabled}" + (f" | file={aud.path} | rotation={cfg['audit']['rotation_mb']}MB"
                f" | retention={cfg['audit']['retention_days']}d" if aud.enabled else " (deduplication.audit.enabled=false)"))
    client = client_from_config('dedup', cfg['ai'])

    tcfg = config['translation']
    tst = translation_settings(tcfg)
    hook = TranslationHook(pool, client_from_config('translate', tcfg), tst)
    dedup_on = cfg['method'] in ('shadow', 'ai')
    logger.info(f"Translation | method={tst.method} | sources={list(tst.sources)} | models={tcfg['models']} | "
                f"upcoming_only={tst.only_upcoming} | key={'set' if tcfg['api_key'] else 'MISSING'}")
    if tst.method != 'off' and not tst.sources:
        logger.warning("TRANSLATE_IDLE | translation.sources пуст: ничего не переводим")
    if tst.method != 'off' and not dedup_on:
        logger.warning("TRANSLATE_IDLE | перевод идёт только после дедупликатора: включи deduplication.method shadow или ai")
    if dedup_on and tst.run_on_start:
        hook.trigger('start')

    reviews_job = ReviewCleanupJob(pool, lambda: config.get('event_reviews') or {})

    try:
        while True:
            try:
                res = await tick(pool, client, cfg, st, on_ok=lambda _res: hook.trigger('dedup_ok'))
                hook.poll()
                await run_after_jobs(res, hook, reviews_job)
            except Exception as e:
                logger.exception(f"DEDUP_SERVICE_TICK_ERROR | {type(e).__name__}: {e}")
            await asyncio.sleep(st.poll_seconds)
    finally:
        await hook.aclose()
        await close_db_pool(pool)
        logger.info("Deduplicator stopped")


if __name__ == '__main__':
    asyncio.run(main())
