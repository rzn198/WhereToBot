"""Разовое скрытие УЖЕ сохранённых событий polako по правилу автоскрытия (event_collector/scrapers/polako.py).

Новые записи скрываются сами при первой вставке; этот скрипт нужен один раз для тех, что уже лежат в базе.
Использует ту же функцию auto_hide_reason, что и скрейпер, — правило в одном месте.

  python -m scripts.apply_polako_autohide            # только список (ничего не меняет)
  python -m scripts.apply_polako_autohide --apply    # скрыть (hidden_by_admin = TRUE) все найденные, ещё не скрытые

Трогает только source='polako' и только записи с hidden_by_admin = FALSE; вернуть можно командой
  UPDATE events SET hidden_by_admin = FALSE WHERE id IN (...);
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('APP_ENV', 'production')

from db.connection import close_db_pool, init_db_pool  # noqa: E402
from event_collector.scrapers.polako import auto_hide_reason  # noqa: E402
from shared.logging import setup_logging  # noqa: E402


async def run(apply: bool) -> int:
    setup_logging('apply_polako_autohide')
    pool = await init_db_pool()
    try:
        rows = await pool.fetch(
            "SELECT id, title, starts_at, venue, hidden_by_admin FROM events WHERE source = 'polako' ORDER BY starts_at, id")
        hit = [(r, auto_hide_reason(r['title'])) for r in rows]
        hit = [(r, why) for r, why in hit if why]
        todo = [r for r, _ in hit if not r['hidden_by_admin']]
        print(f"Под правило подходит {len(hit)} записей polako; уже скрыто {len(hit) - len(todo)}; будет скрыто {len(todo)}")
        by_title: dict[str, list] = {}
        for r, why in hit:
            by_title.setdefault(f"{r['title'][:70]}  [фраза: {why}]", []).append(r)
        for title, rs in by_title.items():
            future = sum(1 for r in rs if r['starts_at'] >= __import__('datetime').date.today())
            print(f"  {len(rs):>3} шт. (будущих {future}): {title}")
        if not apply:
            print("\nРежим просмотра: ничего не изменено. Для скрытия добавьте --apply")
            return 0
        if todo:
            await pool.execute("UPDATE events SET hidden_by_admin = TRUE WHERE id = ANY($1::bigint[]) AND hidden_by_admin = FALSE",
                               [r['id'] for r in todo])
        print(f"\nСкрыто: {len(todo)}")
        return 0
    finally:
        await close_db_pool(pool)


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--apply', action='store_true', help='скрыть найденные записи (по умолчанию только показать)')
    sys.exit(asyncio.run(run(ap.parse_args().apply)))
