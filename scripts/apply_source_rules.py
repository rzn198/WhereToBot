"""Разовое применение правил скрейперов к УЖЕ сохранённым событиям.

Новые записи получают скрытие/метку сами при сохранении; этот скрипт нужен один раз для тех, что уже лежат в базе
(ticketsrs, например, известные записи не пересохраняет вовсе). Использует ТЕ ЖЕ функции, что и скрейперы, —
правила в одном месте (event_collector/scrapers/polako.py, ticketsrs.py).

  python -m scripts.apply_source_rules               # только список (ничего не меняет)
  python -m scripts.apply_source_rules --apply       # применить
  python -m scripts.apply_source_rules --candidates  # записи, похожие на музеи, но вне списка правил (только чтение)

Правила:
  автоскрытие (polako, «Анонимные Алкоголики»): hidden_by_admin = TRUE для ещё не скрытых;
  метка «Музеи» (polako, ticketsrs): event_types = ['Музеи'] там, где категории иные. hidden_by_admin НЕ трогается.
Откат:
  UPDATE events SET hidden_by_admin = FALSE WHERE source='polako' AND title ~* 'анонимные алкоголики';
  метку снимет следующий прогон скрейпера (для известных записей ticketsrs — только пересоздание/правка вручную).
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import defaultdict
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('APP_ENV', 'production')

from db.connection import close_db_pool, init_db_pool  # noqa: E402
from event_collector.normalize import norm_words  # noqa: E402
from event_collector.scrapers import polako, ticketsrs  # noqa: E402
from shared.categories import MUSEUMS  # noqa: E402
from shared.logging import setup_logging  # noqa: E402

HIDE_RULES = {'polako': polako.auto_hide_reason}
MUSEUM_RULES = {'polako': polako.museum_rule, 'ticketsrs': ticketsrs.museum_rule}

# слова-основы, по которым --candidates ищет похожее на музей (только для показа, правил не создаёт)
_MUSEUM_STEMS = ('muzej', 'muzeja', 'muzeju', 'museum', 'музей', 'музе', 'konak', 'galerij', 'zavicajn', 'spomen')


async def _rows(pool):
    return await pool.fetch(
        "SELECT id, source, title, venue, starts_at, event_types, hidden_by_admin FROM events ORDER BY source, starts_at, id")


def _print_groups(title: str, groups: dict) -> None:
    print(title)
    for (source, name, why), rs in sorted(groups.items()):
        future = sum(1 for r in rs if r['starts_at'] >= date.today())
        print(f"  {source:<10} {len(rs):>4} шт. (будущих {future:>3}): {name[:60]}  [правило: {why}]")


async def run(apply: bool, candidates: bool) -> int:
    setup_logging('apply_source_rules')
    pool = await init_db_pool()
    try:
        rows = await _rows(pool)
        hide, label, hide_todo, label_todo = defaultdict(list), defaultdict(list), [], []
        for r in rows:
            why = HIDE_RULES.get(r['source'], lambda t: None)(r['title'])
            if why:
                hide[(r['source'], r['title'], why)].append(r)
                if not r['hidden_by_admin']:
                    hide_todo.append(r['id'])
            why = MUSEUM_RULES.get(r['source'], lambda t: None)(r['title'])
            if why:
                label[(r['source'], r['title'], why)].append(r)
                if list(r['event_types'] or []) != [MUSEUMS]:
                    label_todo.append(r['id'])

        _print_groups("Автоскрытие (hidden_by_admin):", hide)
        print(f"  всего {sum(len(v) for v in hide.values())}, будет скрыто {len(hide_todo)}\n")
        _print_groups(f"Метка «{MUSEUMS}» (event_types):", label)
        print(f"  всего {sum(len(v) for v in label.values())}, будет изменено {len(label_todo)}\n")

        if candidates:
            ruled = {r['id'] for v in label.values() for r in v}
            groups = defaultdict(list)
            for r in rows:
                if r['id'] in ruled or r['starts_at'] < date.today():
                    continue
                tw, vw = norm_words(r['title']), norm_words(r['venue'])
                same = bool(tw) and tw == vw
                musey = any(w.startswith(_MUSEUM_STEMS) for w in tw)
                if same or musey:
                    groups[(r['source'], r['title'], 'название = площадка' if same else 'слово «музей» и т.п.')].append(r)
            _print_groups("Кандидаты вне правил (будущие записи; ничего не меняется):", groups)
            if not groups:
                print("  нет")
            return 0

        if not apply:
            print("Режим просмотра: ничего не изменено. Для применения добавьте --apply")
            return 0
        if hide_todo:
            await pool.execute("UPDATE events SET hidden_by_admin = TRUE WHERE id = ANY($1::bigint[]) AND hidden_by_admin = FALSE",
                               hide_todo)
        if label_todo:
            await pool.execute("UPDATE events SET event_types = ARRAY[$2]::text[] WHERE id = ANY($1::bigint[]) AND NOT category_manual",
                               label_todo, MUSEUMS)
        print(f"Скрыто: {len(hide_todo)}; помечено «{MUSEUMS}»: {len(label_todo)}")
        return 0
    finally:
        await close_db_pool(pool)


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--apply', action='store_true', help='применить правила (по умолчанию только показать)')
    ap.add_argument('--candidates', action='store_true', help='показать похожие на музеи записи вне правил')
    a = ap.parse_args()
    sys.exit(asyncio.run(run(a.apply, a.candidates)))
