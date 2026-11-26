"""AI киноафиши вручную: названия фильмов, склейка, жанры, перевод описаний.

    docker compose exec -T cinema_collector python scripts/cinema_ai_shadow.py --dry-run
    docker compose exec -T cinema_collector python scripts/cinema_ai_shadow.py                 # «тень»: модель вызывается, БД не меняется, CSV
    docker compose exec -T cinema_collector python scripts/cinema_ai_shadow.py --apply         # то же, но результаты записываются в БД
    docker compose exec -T cinema_collector python scripts/cinema_ai_shadow.py --reset-failed  # снять пометку «3 неудачи подряд»

Без --apply в БД НИЧЕГО не пишется (ни названия, ни склейки, ни жанры, ни переводы).
--dry-run — только посчитать, сколько фильмов и символов ушло бы в модель (модель не вызывается).
CSV: <out-dir>/cinema_ai_titles.csv, cinema_ai_genres.csv, cinema_ai_descriptions.csv.

Параметры: --out-dir /tmp | --models a,b | --max-films 40 | --apply | --dry-run | --reset-failed."""
from __future__ import annotations

import argparse
import asyncio
import csv
import os
import sys
from dataclasses import replace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('APP_ENV', 'production')

FIELDS = {
    'title': ('id', 'sessions', 'variants', 'en', 'ru', 'same_as', 'merge_into', 'veto'),
    'genre': ('term', 'genre_ru'),
    'desc': ('id', 'title', 'src_len', 'ru_len', 'note', 'text'),
}
FILES = {'title': 'cinema_ai_titles.csv', 'genre': 'cinema_ai_genres.csv', 'desc': 'cinema_ai_descriptions.csv'}


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--reset-failed', action='store_true')
    ap.add_argument('--out-dir', default='/tmp')
    ap.add_argument('--models', default='')
    ap.add_argument('--max-films', type=int, default=0)
    args = ap.parse_args(argv)
    if args.apply and args.dry_run:
        ap.error('--apply и --dry-run вместе не нужны')
    return args


def write_csvs(rows: list[dict], out_dir: str) -> list[str]:
    paths = []
    for kind, fields in FIELDS.items():
        part = [r for r in rows if r['kind'] == kind]
        if not part:
            continue
        path = os.path.join(out_dir, FILES[kind])
        with open(path, 'w', encoding='utf-8-sig', newline='') as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
            w.writeheader()
            w.writerows(part)
        paths.append(f"{path} ({len(part)})")
    return paths


async def main(argv=None) -> int:
    args = parse_args(argv)
    from cinema_ai import store
    from cinema_ai.service import plan, run_pass
    from cinema_ai.settings import cinema_ai_settings
    from db.connection import close_db_pool, init_db_pool
    from shared.ai import client_from_config
    from shared.config import config

    cfg = dict(config.get('cinema_ai') or {})
    if args.models:
        cfg['models'] = [m.strip() for m in args.models.split(',') if m.strip()]
    st = cinema_ai_settings({**cfg, 'method': 'on'})
    if args.max_films:
        st = replace(st, max_films_per_run=args.max_films)

    pool = await init_db_pool()
    try:
        if args.reset_failed:
            res = await pool.execute("UPDATE cinema_movies SET ai_titles_fails = 0, ai_desc_fails = 0 WHERE ai_titles_fails > 0 OR ai_desc_fails > 0")
            print(f"сброшено неудач: {res}")
            return 0
        movies = await store.load_active(pool)
        a, b = plan(movies, st)
        chars = sum(m['description_src_len'] for m in b)
        print(f"фильмов с будущими сеансами: {len(movies)} | названия (этап A): {len(a)} | переводы (этап B): {len(b)} "
              f"(~{chars} символов, около {chars // 3} токенов на входе) | моделей в цепочке: {len(cfg.get('models') or [])}")
        if args.dry_run:
            return 0
        client = client_from_config('cinema-ai', cfg)
        if not client.configured:
            print("нет OPENCODE_TRANSLATE_API_KEY или моделей — выходим")
            return 1
        try:
            res = await run_pass(pool, client, st, apply=args.apply)
        finally:
            await client.aclose()
        print(('ПРИМЕНЕНО в БД' if args.apply else 'ТЕНЬ: БД не менялась') + f" | {res.line()}")
        print(client.stats.line())
        for p in write_csvs(res.collect, args.out_dir):
            print('CSV:', p)
        return 0 if res.status in ('ok', 'incomplete') else 1
    finally:
        await close_db_pool(pool)


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
