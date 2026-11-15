"""Проверка: решения дедупликатора соответствуют иерархии источников (deduplication.ai.priority).

Берёт из БД действующие решения (status='active') по будущим датам и для каждого сравнивает ранг победителя и
проигравшего по тому же PriorityResolver, что использует движок. Нарушение — проигравший стоит в списке выше
победителя (или оба вне списка, но проигравший — пост канала, а победитель — событие, либо наоборот по правилам
«не указанные ниже всех: сначала посты, затем события»).

    docker compose exec deduplicator python scripts/dedup_check_priority.py
    docker compose exec deduplicator python scripts/dedup_check_priority.py --csv /tmp/prio_violations.csv --all

Показывает:
  1) источники из списка priority, которые не нашлись (каналы без @username в таблице channels);
  2) сводку: сколько решений, сколько нарушений, матрицу «победитель ← проигравший» по источникам;
  3) каждое нарушение с рангами, видом (m/e), id и началом текста;
  4) источники, которых нет в priority (они идут ниже всех).

Нарушение не всегда ошибка: победителем может быть «липкий» победитель (решение принято раньше, когда более
высокого источника ещё не было), группа может содержать запись вне списка с более длинным текстом и т. п.
Скрипт ничего не меняет в БД.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from deduplicator import store  # noqa: E402
from deduplicator.priority import UNLISTED, PriorityResolver  # noqa: E402
from deduplicator.runner import ai_settings  # noqa: E402


def classify(resolver: PriorityResolver, winner, loser) -> tuple[bool, str]:
    """(нарушение?, пояснение). Сравниваем так же, как движок: по rank без длины текста и даты."""
    wp, lp = resolver.position(winner), resolver.position(loser)
    if lp < wp:
        return True, 'проигравший выше в списке'
    if lp == wp == UNLISTED:
        wk = resolver.rank(winner)[:2]
        lk = resolver.rank(loser)[:2]
        if lk < wk:
            return True, 'оба вне списка: пост должен побеждать событие'
        return False, 'оба вне списка'
    return False, ''


async def run(args) -> int:
    from db.connection import close_db_pool, init_db_pool
    from shared.config import config

    pool = await init_db_pool()
    try:
        s = ai_settings(config['deduplication'])
        names = await store.load_channel_names(pool)
        resolver = PriorityResolver(s['priority'], names)
        today = await pool.fetchval("SELECT CURRENT_DATE")
        end = await pool.fetchval("SELECT CURRENT_DATE + 400")
        records = {r.key: r for r in await store.load_records(pool, today, end)}
        rows = await pool.fetch(
            "SELECT id, day, winner_kind, winner_id, loser_kind, loser_id, decided_by, model, reason "
            "FROM dedup_decisions WHERE status = 'active' AND day >= CURRENT_DATE ORDER BY day, id")
    finally:
        await close_db_pool(pool)

    print(f"Приоритет: {len(resolver.entries)} записей в списке")
    bad = resolver.unresolved()
    if bad:
        print(f"  ! не найдены в таблице channels (идут как «не указанные»): {bad}")

    total = 0
    viol: list[dict] = []
    matrix: Counter = Counter()
    unlisted: Counter = Counter()
    missing = 0
    for d in rows:
        w = records.get((d['winner_kind'], d['winner_id']))
        l = records.get((d['loser_kind'], d['loser_id']))
        if w is None or l is None:
            missing += 1
            continue
        total += 1
        matrix[(resolver.label(w), resolver.label(l))] += 1
        for r in (w, l):
            if resolver.position(r) == UNLISTED:
                unlisted[resolver.label(r)] += 1
        is_bad, why = classify(resolver, w, l)
        if is_bad or args.all:
            viol.append(dict(
                id=d['id'], day=d['day'], violation='ДА' if is_bad else '', why=why, decided_by=d['decided_by'],
                winner=f"{w.kind}{w.id}", winner_src=resolver.label(w), winner_pos=resolver.position(w),
                loser=f"{l.kind}{l.id}", loser_src=resolver.label(l), loser_pos=resolver.position(l),
                reason=(d['reason'] or '')[:100],
                winner_text=(w.text or '')[:100].replace('\n', ' '), loser_text=(l.text or '')[:100].replace('\n', ' ')))
    n_bad = sum(1 for v in viol if v['violation'])
    print(f"Активных решений: {len(rows)} (проверено {total}, без записи в БД {missing}); нарушений порядка: {n_bad}")

    print("\nПобедитель ← проигравший (число решений):")
    for (ws, ls), n in matrix.most_common(40):
        print(f"  {n:4d}  {ws}  ←  {ls}")

    if n_bad or args.all:
        print("\nНарушения:" if not args.all else "\nВсе решения:")
        for v in viol:
            mark = '!!' if v['violation'] else '  '
            print(f"{mark} #{v['id']} {v['day']} {v['winner']}[{v['winner_src']} #{v['winner_pos']}] ← "
                  f"{v['loser']}[{v['loser_src']} #{v['loser_pos']}] {v['why']}")
            print(f"     W: {v['winner_text']}\n     L: {v['loser_text']}")

    if unlisted:
        print("\nИсточники вне списка priority (ниже всех), число участий в решениях:")
        for k, n in unlisted.most_common(30):
            print(f"  {n:4d}  {k}")

    if args.csv:
        with open(args.csv, 'w', newline='', encoding='utf-8') as f:
            wr = csv.DictWriter(f, fieldnames=list(viol[0].keys()) if viol else ['id'])
            wr.writeheader()
            wr.writerows(viol)
        print(f"\nCSV: {args.csv}")
    return 0 if not n_bad else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--all', action='store_true', help='показать все решения, а не только нарушения')
    p.add_argument('--csv', help='записать список в CSV')
    return asyncio.run(run(p.parse_args(argv)))


if __name__ == '__main__':
    sys.exit(main())
