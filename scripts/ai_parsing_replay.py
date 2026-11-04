"""Прогон AI-парсинга по УЖЕ сохранённым messages и сверка с тем, что лежит в БД.
В БД ничего не пишет. Нужен, чтобы оценить качество модели до включения shadow/ai.

    python scripts/ai_parsing_replay.py --limit 200
    python scripts/ai_parsing_replay.py --limit 500 --days 30 --channel 123456789 --concurrency 3

Нужны: OPENCODE_PARSING_API_KEY в окружении и `parsing.models` в config.yaml.
Результат: сводка в консоль и ai_replay.csv (все строки) рядом со скриптом.
"""
import argparse
import asyncio
import csv
import os
import sys
import time
from pathlib import Path

if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault('APP_ENV', 'local')

from collector.ai import extractor  # noqa: E402
from collector.categorizer_v2 import category_names  # noqa: E402


async def replay(pool, client, limit, days=None, channel_id=None, concurrency=3, max_chars=2000):
    where, args = ["text <> ''"], []
    if days:
        args.append(days)
        where.append(f"published_at >= now() - ($%d || ' days')::interval" % len(args))
    if channel_id:
        args.append(channel_id)
        where.append("channel_id = $%d" % len(args))
    args.append(limit)
    rows = await pool.fetch(
        f"SELECT surrogate_id, channel_id, position, text, date, published_at, event_types FROM messages "
        f"WHERE {' AND '.join(where)} ORDER BY published_at DESC LIMIT ${len(args)}", *args)

    allowed = category_names()
    sem = asyncio.Semaphore(concurrency)
    out = []

    async def one(r):
        async with sem:
            res, err = None, ''
            try:
                res = await extractor.extract(client, r['text'], r['published_at'].date(), allowed, max_chars)
            except extractor.BadAIOutput as e:
                err = f'bad_output: {e}'
            except Exception as e:                       # AIUnavailable и прочее
                err = f'{type(e).__name__}: {e}'
            stored = list(r['event_types'] or [])
            out.append(dict(
                id=r['surrogate_id'], channel_id=r['channel_id'], position=r['position'], published=r['published_at'].date().isoformat(),
                stored_date=r['date'].isoformat() if r['date'] else '', ai_date=(res.date.isoformat() if res and res.date else ''),
                date_match='' if res is None else str((res.date == r['date'])),
                stored_cats='; '.join(stored), ai_cats='; '.join(res.categories) if res else '',
                cats_match='' if res is None else str(set(res.categories) == set(stored)),
                error=err, text=' '.join(r['text'].split())[:100]))
    await asyncio.gather(*[one(r) for r in rows])
    out.sort(key=lambda x: -x['id'])
    return out


def summarize(rows, client, seconds):
    """position > 0 — строки кастом-парсера (дайджесты): в проде AI не переопределяет их дату
    и категорию, поэтому в сравнение качества они не входят (показаны отдельно)."""
    custom = [r for r in rows if int(r['position'] or 0) > 0]
    real = [r for r in rows if int(r['position'] or 0) == 0]
    ok = [r for r in real if not r['error']]
    n = len(rows)
    dm = sum(1 for r in ok if r['date_match'] == 'True')
    cm = sum(1 for r in ok if r['cats_match'] == 'True')
    ai_found_stored_none = sum(1 for r in ok if r['ai_date'] and not r['stored_date'])
    stored_found_ai_none = sum(1 for r in ok if r['stored_date'] and not r['ai_date'])
    both_differ = sum(1 for r in ok if r['ai_date'] and r['stored_date'] and r['ai_date'] != r['stored_date'])
    print(f"\nСообщений: {n} (из них строк кастом-парсера, не входят в сравнение: {len(custom)}) | "
          f"в сравнении: {len(real)} | ответов AI: {len(ok)} | ошибок: {len(real) - len(ok)} | {seconds:.0f} с")
    if ok:
        print(f"Даты совпали:       {dm}/{len(ok)} ({100 * dm / len(ok):.0f}%)")
        print(f"  AI нашёл, в БД нет:     {ai_found_stored_none}")
        print(f"  в БД есть, AI «нет»:    {stored_found_ai_none}")
        print(f"  обе есть, но разные:    {both_differ}")
        print(f"Категории совпали:  {cm}/{len(ok)} ({100 * cm / len(ok):.0f}%)")
    rows = real
    print("Токены:", client.stats.line())
    errs = {}
    for r in rows:
        if r['error']:
            errs[r['error'][:60]] = errs.get(r['error'][:60], 0) + 1
    for e, c in sorted(errs.items(), key=lambda x: -x[1])[:5]:
        print(f"  ошибка ×{c}: {e}")
    print("\nРасхождения по датам (обе даты есть):")
    for r in [r for r in ok if r['ai_date'] and r['stored_date'] and r['ai_date'] != r['stored_date']][:12]:
        print(f"  #{r['id']} БД={r['stored_date']} AI={r['ai_date']} | {r['text']}")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=200)
    ap.add_argument('--days', type=int)
    ap.add_argument('--channel', type=int, help='channel_id')
    ap.add_argument('--concurrency', type=int, default=3)
    args = ap.parse_args()

    from shared.config import config
    from shared.ai import client_from_config
    from db.connection import init_db_pool, close_db_pool
    from loguru import logger
    logger.remove()
    logger.add(sys.stderr, level='WARNING')

    cfg = config['parsing']
    client = client_from_config('parsing', cfg)
    if not client.configured:
        sys.exit("Нет OPENCODE_PARSING_API_KEY или parsing.models в config.yaml")
    pool = await init_db_pool()
    try:
        t = time.perf_counter()
        rows = await replay(pool, client, args.limit, args.days, args.channel, args.concurrency, cfg['max_chars'])
        summarize(rows, client, time.perf_counter() - t)
        path = Path(__file__).with_name('ai_replay.csv')
        with open(path, 'w', newline='', encoding='utf-8-sig') as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ['id'])
            w.writeheader()
            w.writerows(rows)
        print(f"\nCSV: {path}")
    finally:
        await client.aclose()
        await close_db_pool(pool)


if __name__ == '__main__':
    asyncio.run(main())
