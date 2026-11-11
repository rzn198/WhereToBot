"""Разовое обновление событий одного источника: заново читает все детальные страницы и сохраняет через обычный upsert.

Нужно после правки скрейпера, чтобы исправить уже сохранённые события (например, описания gigstix), не дожидаясь, пока
событие попадёт в окно refresh_upcoming_days. Upsert сам обнуляет description_ru, если описание изменилось, и не трогает
hidden_by_admin и решения дедупликатора.

    docker compose exec event_collector python scripts/refresh_source.py gigstix --dry-run
    docker compose exec event_collector python scripts/refresh_source.py gigstix

--dry-run: ничего не пишет, показывает сколько описаний изменится и несколько примеров «было → стало».

AI-категории на разовый запуск, без правки config.yaml:
    ... refresh_source.py ticketsrs --dry-run --ai shadow --scope all    # посмотреть, что изменил бы AI на ВСЕХ событиях
    ... refresh_source.py ticketsrs --ai ai --scope all                  # применить к ВСЕМ событиям источника
Источники: efinityrs, polako, biletrs, ticketsrs, ticrs, balkan-promo, gigstix.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('APP_ENV', 'production')

from loguru import logger  # noqa: E402

from db.connection import close_db_pool, init_db_pool  # noqa: E402
from event_collector.runner import SCRAPERS, _call_fetch  # noqa: E402
from event_collector.saver import save_events  # noqa: E402
from shared.logging import setup_logging  # noqa: E402


def _category_ai(args):
    """Сервис AI-категорий с разовыми переопределениями из ключей --ai / --scope (конфиг не меняем)."""
    from event_collector.ai_categories import EventCategoryAI
    from shared.config import config
    cfg = dict(config.get('event_categories') or {})
    if args.ai:
        cfg['method'] = args.ai
    if args.scope:
        cfg['scope'] = args.scope
    return EventCategoryAI(cfg=cfg)


async def run(args) -> int:
    by_name = {getattr(c, 'source', c.__name__): c for c in SCRAPERS}
    cls = by_name.get(args.source)
    if cls is None:
        print(f"Неизвестный источник {args.source!r}. Доступны: {', '.join(sorted(by_name))}")
        return 2

    setup_logging('refresh_source')
    pool = await init_db_pool()
    try:
        scraper = cls()
        items = await _call_fetch(scraper, {})          # пустой known — читаем все страницы
        print(f"Прочитано страниц: {len(items)}")
        old = {r['source_url']: r['description'] for r in await pool.fetch(
            "SELECT source_url, description FROM events WHERE lower(source) = $1", args.source.lower())}
        changed = [(it, old[it.source_url]) for it in items
                   if it.source_url in old and (it.description or '') != (old[it.source_url] or '')]
        grew = [c for c in changed if len(c[0].description or '') > len(c[1] or '')]
        new = [it for it in items if it.source_url not in old]
        print(f"В базе событий источника: {len(old)}; описание изменится у {len(changed)} "
              f"(длиннее у {len(grew)}); новых событий {len(new)}")
        for it, was in changed[:args.examples]:
            print(f"\n  {it.source_url}\n    было  ({len(was or '')}): {(was or '')[:90]!r}\n"
                  f"    стало ({len(it.description or '')}): {(it.description or '')[:90]!r}")
        ai = _category_ai(args)
        if ai.enabled:
            counters = await ai.apply(pool, items)
            print(f"\nAI-категории ({ai.mode}/{ai.scope}): {counters.line()}")
            for title, url, was, now in counters.diffs[:args.examples * 4]:
                print(f"  {title[:60]!r}: {was} → {now}")
        if args.dry_run:
            print("\nСухой прогон: в базу ничего не записано (кэш AI-ответов мог пополниться).")
            return 0
        saved = await save_events(pool, items)
        print(f"\nСохранено (insert+update): {saved}")
        return 0
    finally:
        await close_db_pool(pool)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('source')
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--ai', choices=['source', 'shadow', 'ai'], help='режим AI-категорий на этот запуск (по умолчанию из конфига)')
    p.add_argument('--scope', choices=['unreliable', 'all'], help='unreliable — только ненадёжные категории; all — все события')
    p.add_argument('--examples', type=int, default=5, help='сколько примеров «было → стало» показать')
    return asyncio.run(run(p.parse_args(argv)))


if __name__ == '__main__':
    sys.exit(main())
