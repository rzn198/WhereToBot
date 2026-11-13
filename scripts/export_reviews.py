"""Выгрузка пересмотров событий (таблица event_reviews) в CSV или JSONL.

    docker compose exec bot python scripts/export_reviews.py                    # открытые, CSV → /app/logs/event_reviews.csv
    docker compose exec bot python scripts/export_reviews.py --status all --format jsonl
    docker compose cp bot:/app/logs/event_reviews.csv ./event_reviews.csv

Закрыть пересмотр (после разбора) — в БД:
    UPDATE event_reviews SET status='resolved', resolved_at=now(), resolution_note='...' WHERE id = 12;
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('APP_ENV', 'production')

from db.connection import close_db_pool, init_db_pool  # noqa: E402

COLUMNS = ['id', 'created_at', 'status', 'kind', 'reason_code', 'reason_text', 'correct_category', 'correct_date',
           'title', 'starts_at', 'venue', 'source', 'source_url', 'event_types', 'description',
           'event_id', 'message_id', 'resolved_at', 'resolution_note']


def _cell(v):
    if isinstance(v, list):
        return '|'.join(v)
    return '' if v is None else str(v)


async def run(args) -> int:
    pool = await init_db_pool()
    try:
        where = '' if args.status == 'all' else f"WHERE status = '{args.status}'"
        rows = await pool.fetch(f"SELECT {', '.join(COLUMNS)}, snapshot FROM event_reviews {where} ORDER BY id")
    finally:
        await close_db_pool(pool)
    out = args.out or f"/app/logs/event_reviews.{'jsonl' if args.format == 'jsonl' else 'csv'}"
    os.makedirs(os.path.dirname(out) or '.', exist_ok=True)
    if args.format == 'jsonl':
        with open(out, 'w', encoding='utf-8') as f:
            for r in rows:
                d = {k: (v.isoformat() if hasattr(v, 'isoformat') else v) for k, v in dict(r).items()}
                if isinstance(d.get('snapshot'), str):
                    d['snapshot'] = json.loads(d['snapshot'])
                f.write(json.dumps(d, ensure_ascii=False) + '\n')
    else:
        with open(out, 'w', encoding='utf-8-sig', newline='') as f:
            w = csv.writer(f)
            w.writerow(COLUMNS)
            for r in rows:
                w.writerow([_cell(r[c]) for c in COLUMNS])
    print(f"Записано {len(rows)} пересмотров → {out}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--status', choices=['open', 'resolved', 'all'], default='open')
    p.add_argument('--format', choices=['csv', 'jsonl'], default='csv')
    p.add_argument('--out')
    return asyncio.run(run(p.parse_args(argv)))


if __name__ == '__main__':
    sys.exit(main())
