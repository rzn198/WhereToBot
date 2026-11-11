"""Проверка AI-категорий на реальных событиях из БД. Только чтение: в events ничего не пишет, кэш AI не трогает,
на сайты не ходит. Показывает, где ответ AI расходится с категориями, которые сейчас лежат в базе.

    docker compose exec event_collector python scripts/check_db_categories.py ticketsrs --limit 40
    docker compose exec event_collector python scripts/check_db_categories.py gigstix ticrs polako --limit 30
    docker compose exec event_collector python scripts/check_db_categories.py --all --limit 20   # по 20 из каждого источника

Выбираются будущие события случайно. Запрос модели получает текущие категории из базы как подсказку (в рабочем режиме
подсказка — категория сайта), поэтому точное воспроизведение рабочих ответов даёт только пробный режим shadow.
--models a,b — цепочка моделей на этот запуск. Печатаются только расхождения; совпавшие считаются в итоге.
--title SENIDAH --show-text 400 — разобрать конкретное событие: что в описании и что ответил AI."""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('APP_ENV', 'production')

from db.connection import close_db_pool, init_db_pool  # noqa: E402
from event_collector.ai_categories import EventCategoryAI  # noqa: E402
from event_collector.scrapers.base import EventItem  # noqa: E402
from shared.config import config  # noqa: E402

SOURCES = ['efinityrs', 'polako', 'biletrs', 'ticketsrs', 'ticrs', 'balkan-promo', 'gigstix']


async def main_async(args) -> int:
    sources = SOURCES if args.all else args.sources
    if not sources:
        print("Укажите источники (например: ticketsrs gigstix) или --all.")
        return 2
    cfg = dict(config.get('event_categories') or {})
    if args.models:
        cfg['models'] = [m.strip() for m in args.models.split(',')]
    if not cfg.get('api_key') or not cfg.get('models'):
        print("Нет ключа или моделей: OPENCODE_EVENTS_API_KEY / OPENCODE_PARSING_API_KEY и --models либо "
              "event_collector.categories.models в config.yaml.")
        return 2
    cfg.update(method='shadow', scope='all', max_calls_per_run=args.limit * len(sources))

    pool = await init_db_pool()
    try:
        total = agree = 0
        for src in sources:
            rows = await pool.fetch(
                "SELECT source, source_url, title, venue, description, event_types FROM events "
                "WHERE lower(source) = $1 AND starts_at >= now() AND ($3::text IS NULL OR title ILIKE '%' || $3 || '%') "
                "ORDER BY random() LIMIT $2", src.lower(), args.limit, args.title)
            items = [EventItem(source=r['source'], source_url=r['source_url'], title=r['title'],
                               starts_at=datetime.now(), description=r['description'], venue=r['venue'],
                               event_types=list(r['event_types'] or []), category_basis='none',
                               category_hint="сейчас в базе: " + (", ".join(r['event_types'] or []) or "пусто"))
                     for r in rows]
            if not items:
                print(f"\n=== {src}: будущих событий нет")
                continue
            ai = EventCategoryAI(cfg=cfg)
            c = await ai.apply(None, items)          # pool=None: без кэша, в БД ничего не пишется
            print(f"\n=== {src}: проверено {len(items)}; AI ответил {c.ok}; ошибок {c.failed}; расхождений {c.disagree}")
            texts = {it.title: it.description for it in items}
            for title, _url, was, now in c.diffs:
                print(f"  ≠ {title[:60]!r}\n      база: {was}\n      AI:   {now}")
                if args.show_text:
                    print(f"      текст: {(texts.get(title) or '—')[:args.show_text]!r}")
            total += c.ok
            agree += c.ok - c.disagree
        print(f"\nИтого: AI ответил по {total}, совпало с базой {agree}, расходится {total - agree}.")
        return 0
    finally:
        await close_db_pool(pool)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('sources', nargs='*')
    p.add_argument('--all', action='store_true')
    p.add_argument('--limit', type=int, default=30, help='событий на источник')
    p.add_argument('--models')
    p.add_argument('--title', help='только события, в названии которых есть эта подстрока (без учёта регистра)')
    p.add_argument('--show-text', type=int, default=0, metavar='N', help='к каждому расхождению печатать первые N символов описания')
    return asyncio.run(main_async(p.parse_args(argv)))


if __name__ == '__main__':
    sys.exit(main())
