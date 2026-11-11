"""Проверка AI-категорий на реальных событиях из БД. Только чтение: в events ничего не пишет, кэш AI не трогает,
на сайты не ходит. Показывает, где ответ AI расходится с категориями, которые сейчас лежат в базе.

    docker compose exec event_collector python scripts/check_db_categories.py ticketsrs --limit 40
    docker compose exec event_collector python scripts/check_db_categories.py gigstix ticrs polako --limit 30
    docker compose exec event_collector python scripts/check_db_categories.py --all --limit 20   # по 20 из каждого источника

Выбираются будущие события случайно. Запрос модели получает текущие категории из базы как подсказку (в рабочем режиме
подсказка — категория сайта), поэтому точное воспроизведение рабочих ответов даёт только пробный режим shadow.
--models a,b — цепочка моделей на этот запуск. Печатаются только расхождения; совпавшие считаются в итоге.
--compare deepseek-v4.1-flash,gpt-5.6-luna — сравнить модели на одной и той же выборке.
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


def _items(rows) -> list[EventItem]:
    return [EventItem(source=r['source'], source_url=r['source_url'], title=r['title'],
                      starts_at=datetime.now(), description=r['description'], venue=r['venue'],
                      event_types=list(r['event_types'] or []), category_basis='none',
                      category_hint="сейчас в базе: " + (", ".join(r['event_types'] or []) or "пусто"))
            for r in rows]


async def compare(args, cfg: dict, sources: list[str]) -> int:
    """Одна и та же выборка событий, каждая модель отвечает отдельно (без запасных моделей)."""
    models = [m.strip() for m in args.compare.split(',') if m.strip()]
    pool = await init_db_pool()
    try:
        stat = {m: dict(ok=0, fail=0, same_db=0) for m in models}
        pair_same = pair_total = 0
        for src in sources:
            rows = await pool.fetch(
                "SELECT source, source_url, title, venue, description, event_types FROM events "
                "WHERE lower(source) = $1 AND starts_at >= now() AND ($3::text IS NULL OR title ILIKE '%' || $3 || '%') "
                "ORDER BY random() LIMIT $2", src.lower(), args.limit, args.title)
            if not rows:
                print(f"\n=== {src}: будущих событий нет")
                continue
            base = {r['source_url']: (r['title'], list(r['event_types'] or [])) for r in rows}
            answers: dict[str, dict[str, list[str] | None]] = {m: {} for m in models}
            for m in models:
                items = _items(rows)
                c = await EventCategoryAI(cfg={**cfg, 'models': [m]}).apply(None, items)
                final = {url: now for _t, url, _was, now in c.diffs}
                answered = c.ok
                stat[m]['ok'] += answered
                stat[m]['fail'] += len(items) - answered
                for it in items:
                    # ответил и не спорит с базой → итог = база; ответа нет (сбой) → None
                    answers[m][it.source_url] = (final.get(it.source_url, base[it.source_url][1])
                                                 if it.source_url in c.answered else None)
            print(f"\n=== {src}: событий {len(rows)}")
            for url, (title, db) in base.items():
                got = {m: answers[m][url] for m in models}
                pair_total += 1
                key = {m: (None if got[m] is None else frozenset(got[m])) for m in models}
                same_models = len(set(key.values())) == 1 and None not in key.values()
                pair_same += same_models
                for m in models:
                    stat[m]['same_db'] += key[m] == frozenset(db)
                if same_models and key[models[0]] == frozenset(db):
                    continue
                print(f"  ≠ {title[:60]!r}\n      база: {db}")
                for m in models:
                    print(f"      {m}: {got[m] if got[m] is not None else 'нет ответа (сбой модели)'}")
                if same_models:
                    print("      (модели согласны между собой, спорят с базой)")
                if args.show_text:
                    d = next(r['description'] for r in rows if r['source_url'] == url)
                    print(f"      текст: {(d or '—')[:args.show_text]!r}")
        print(f"\nИтого событий {pair_total}; модели ответили одинаково на {pair_same}.")
        for m in models:
            s = stat[m]
            print(f"  {m}: ответов {s['ok']}, сбоев {s['fail']}, совпало с базой {s['same_db']}")
        return 0
    finally:
        await close_db_pool(pool)


async def main_async(args) -> int:
    sources = SOURCES if args.all else args.sources
    if not sources:
        print("Укажите источники (например: ticketsrs gigstix) или --all.")
        return 2
    cfg = dict(config.get('event_categories') or {})
    if args.models:
        cfg['models'] = [m.strip() for m in args.models.split(',')]
    if not cfg.get('api_key') or not (cfg.get('models') or args.compare):
        print("Нет ключа или моделей: OPENCODE_EVENTS_API_KEY / OPENCODE_PARSING_API_KEY и --models либо "
              "event_collector.categories.models в config.yaml.")
        return 2
    cfg.update(method='shadow', scope='all', max_calls_per_run=args.limit * len(sources))

    if args.compare:
        return await compare(args, cfg, sources)
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
    p.add_argument('--compare', metavar='A,B', help='сравнить модели на одной выборке: --compare deepseek-v4.1-flash,gpt-5.6-luna')
    p.add_argument('--title', help='только события, в названии которых есть эта подстрока (без учёта регистра)')
    p.add_argument('--show-text', type=int, default=0, metavar='N', help='к каждому расхождению печатать первые N символов описания')
    return asyncio.run(main_async(p.parse_args(argv)))


if __name__ == '__main__':
    sys.exit(main())
