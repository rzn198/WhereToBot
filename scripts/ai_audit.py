"""Проверка AI-категоризации на выборке событий из аудита (polako, ticketsrs): чистый ответ AI БЕЗ слияния с категориями
сайта, ничего не пишет в events и в кэш event_ai_categories.

    docker compose exec event_collector python scripts/ai_audit.py
    docker compose cp event_collector:/app/logs/ai_audit.csv ./ai_audit.csv      # (или scp из тома logs)

Список id — scripts/audit_ids.txt (те же 1166 записей, что в разметке). Цепочка моделей и max_chars — из
event_categories в config.yaml. Подсказки сайта (category_hint) в БД не хранятся, поэтому AI видит название, площадку и
описание — это строже, чем в боевом прогоне. Одинаковые по тексту записи спрашиваются один раз.
Колонки CSV: id, source, title, venue, db, ai.
"""
from __future__ import annotations

import asyncio
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('APP_ENV', 'production')

from loguru import logger  # noqa: E402

from collector.categorizer_v2 import category_names  # noqa: E402
from db.connection import close_db_pool, init_db_pool  # noqa: E402
from event_collector.ai_categories import SYSTEM_PROMPT, build_user_prompt, parse_response  # noqa: E402
from event_collector.scrapers.base import EventItem  # noqa: E402
from shared.ai import AIUnavailable, client_from_config  # noqa: E402
from shared.config import config  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.environ.get('AI_AUDIT_OUT', '/app/logs/ai_audit.csv')


async def main() -> int:
    ids = [int(x) for x in open(os.path.join(HERE, 'audit_ids.txt')).read().split()]
    cfg = dict(config.get('event_categories') or {})
    max_chars = int(cfg.get('max_chars', 1200))
    conc = max(1, int(cfg.get('concurrency', 3)))
    pool = await init_db_pool()
    try:
        rows = await pool.fetch(
            "SELECT id, source, title, venue, description, event_types FROM events WHERE id = ANY($1::int[]) ORDER BY id", ids)
    finally:
        await close_db_pool(pool)
    print(f"Записей из БД: {len(rows)} из {len(ids)}")

    client = client_from_config('event-categories', cfg)
    client.begin_run()
    allowed = category_names()
    answers: dict[tuple, list[str] | None] = {}
    sem = asyncio.Semaphore(conc)
    done = 0

    async def ask(key: tuple, item: EventItem) -> None:
        nonlocal done
        async with sem:
            if not client.available:
                answers[key] = None
                return
            try:
                raw = await client.complete(SYSTEM_PROMPT, build_user_prompt(item, allowed, max_chars))
                answers[key] = parse_response(raw, allowed)
                client.report_ok()
            except AIUnavailable as e:
                logger.warning(f"AI_AUDIT_UNAVAILABLE | {e}")
                answers[key] = None
            except Exception as e:
                logger.warning(f"AI_AUDIT_BAD | {item.title[:40]!r} | {type(e).__name__}: {e}")
                answers[key] = None
            done += 1
            if done % 50 == 0:
                print(f"  готово {done}")

    items: dict[tuple, EventItem] = {}
    for r in rows:
        key = (r['title'], r['venue'] or '', (r['description'] or '')[:max_chars])
        if key not in items:
            items[key] = EventItem(source=r['source'], source_url=str(r['id']), title=r['title'], starts_at=None,
                                   description=r['description'], venue=r['venue'])
    print(f"Уникальных запросов к AI: {len(items)}")
    await asyncio.gather(*(ask(k, it) for k, it in items.items()))
    await client.aclose()

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    fails = 0
    with open(OUT, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(['id', 'source', 'title', 'venue', 'db', 'ai'])
        for r in rows:
            ai = answers.get((r['title'], r['venue'] or '', (r['description'] or '')[:max_chars]))
            fails += ai is None
            w.writerow([r['id'], r['source'], r['title'], r['venue'] or '', '|'.join(r['event_types'] or []),
                        '|'.join(ai) if ai else ''])
    print(f"Записано {OUT}; без ответа AI: {fails}")
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
