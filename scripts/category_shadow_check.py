"""Проверка категорий событий через AI «в тени»: читает события из БД, спрашивает AI теми же промптом и моделями,
что и сбор событий, и пишет в CSV сравнение «в базе → ответ AI». В БД НИЧЕГО не пишет (ни events, ни кэш event_ai_categories),
сайты не опрашивает.

    docker compose exec -T event_collector python scripts/category_shadow_check.py --dry-run
    docker compose exec -T event_collector python scripts/category_shadow_check.py --sources polako,ticketsrs --out /tmp/cat_shadow.csv

По умолчанию: события Белграда, не скрытые, с датой не раньше сегодняшней, кроме «Музеев» и записей с ручной категорией.
Одинаковые по тексту записи (название + описание + площадка, разные даты) спрашиваются один раз.
Оговорка: подсказки «Категория на сайте» (category_hint) в БД нет, поэтому AI отвечает только по тексту; в боевом сборе
подсказка есть, и ответы могут чуть отличаться.

Параметры: --sources polako,ticketsrs | --stored "Игры и развлечения" (только записи с такой главной категорией в базе) |
--ids 44157,533 | --from-date ГГГГ-ММ-ДД | --city Belgrade | --limit N | --max-calls 1500 | --concurrency 3 | --models a,b |
--out файл.csv | --dry-run (только посчитать запросы)."""
from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import os
import sys
from collections import Counter
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('APP_ENV', 'production')

DEFAULT_MAX_CALLS = 1500


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--sources', default='')
    ap.add_argument('--stored', default='')
    ap.add_argument('--ids', default='')
    ap.add_argument('--from-date', default='')
    ap.add_argument('--city', default='Belgrade')
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--max-calls', type=int, default=DEFAULT_MAX_CALLS)
    ap.add_argument('--concurrency', type=int, default=0)
    ap.add_argument('--models', default='')
    ap.add_argument('--out', default='/tmp/cat_shadow.csv')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args(argv)
    if args.from_date:
        try:
            date.fromisoformat(args.from_date)
        except ValueError:
            ap.error('--from-date: формат ГГГГ-ММ-ДД')
    if args.ids:
        try:
            [int(x) for x in args.ids.split(',') if x.strip()]
        except ValueError:
            ap.error('--ids: числа через запятую')
    return args


def text_key(row: dict, max_chars: int) -> str:
    """Ключ «одинаковый по тексту»: название + площадка + начало описания."""
    raw = f"{row['title']}|{row['venue'] or ''}|{(row['description'] or '')[:max_chars]}"
    return hashlib.md5(raw.encode('utf-8')).hexdigest()


def group_rows(rows: list[dict], max_chars: int) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(text_key(r, max_chars), []).append(r)
    return groups


def compare(stored: list[str], ai: list[str]) -> tuple[bool, bool]:
    """(главная категория совпала, наборы совпали)."""
    return bool(stored and ai and stored[0] == ai[0]), set(stored) == set(ai)


async def fetch_rows(pool, args) -> list[dict]:
    from shared.tz import belgrade_today  # noqa: PLC0415
    start = date.fromisoformat(args.from_date) if args.from_date else belgrade_today()
    where = ["NOT hidden_by_admin", "NOT category_manual", "city = $1", "starts_at >= $2",
             "NOT ('Музеи' = ANY(event_types))"]
    params: list = [args.city, start]
    if args.sources:
        params.append([s.strip().lower() for s in args.sources.split(',') if s.strip()])
        where.append(f"lower(source) = ANY(${len(params)}::text[])")
    if args.ids:
        params.append([int(x) for x in args.ids.split(',') if x.strip()])
        where.append(f"id = ANY(${len(params)}::bigint[])")
    if args.stored:
        params.append(args.stored)
        where.append(f"event_types[1] = ${len(params)}")
    sql = ("SELECT id, source, source_url, title, venue, description, event_types, starts_at FROM events "
           "WHERE " + " AND ".join(where) + " ORDER BY id")
    if args.limit:
        sql += f" LIMIT {int(args.limit)}"
    return [dict(r) for r in await pool.fetch(sql, *params)]


async def ask_all(groups: dict[str, list[dict]], client, allowed: list[str], max_chars: int, max_calls: int,
                  concurrency: int) -> dict[str, dict]:
    from collector.ai.extractor import BadAIOutput  # noqa: PLC0415
    from event_collector.ai_categories import SYSTEM_PROMPT, build_user_prompt, parse_response  # noqa: PLC0415
    from event_collector.scrapers.base import EventItem  # noqa: PLC0415
    from shared.ai import AIUnavailable  # noqa: PLC0415
    results: dict[str, dict] = {}
    state = {'calls': 0, 'down': False}
    sem = asyncio.Semaphore(max(1, concurrency))
    client.begin_run()

    async def one(key: str, rows: list[dict]) -> None:
        async with sem:
            if state['down'] or not client.available:
                state['down'] = True
                results[key] = {'ai': [], 'model': '', 'error': 'модели недоступны'}
                return
            if state['calls'] >= max_calls:
                results[key] = {'ai': [], 'model': '', 'error': 'лимит запросов'}
                return
            state['calls'] += 1
            r = rows[0]
            item = EventItem(source=r['source'], source_url=r['source_url'], title=r['title'],
                             starts_at=datetime.combine(r['starts_at'], datetime.min.time()),
                             description=r['description'], venue=r['venue'])
            try:
                raw = await client.complete(SYSTEM_PROMPT, build_user_prompt(item, allowed, max_chars))
                cats = parse_response(raw, allowed)
            except AIUnavailable as e:
                state['down'] = True
                results[key] = {'ai': [], 'model': '', 'error': f'недоступно: {e}'}
                return
            except BadAIOutput as e:
                client.report_bad_output()
                results[key] = {'ai': [], 'model': client.current_model or '', 'error': f'плохой ответ: {e}'}
                return
            client.report_ok()
            results[key] = {'ai': cats, 'model': client.current_model or '', 'error': ''}

    await asyncio.gather(*(one(k, v) for k, v in groups.items()))
    return results


def write_csv(path: str, rows: list[dict], results: dict[str, dict], max_chars: int) -> list[dict]:
    out = []
    for r in rows:
        res = results.get(text_key(r, max_chars), {'ai': [], 'model': '', 'error': 'не спрошено'})
        stored = list(r['event_types'] or [])
        same_first, same_set = compare(stored, res['ai'])
        out.append({'id': r['id'], 'source': r['source'], 'day': r['starts_at'], 'title': r['title'],
                    'venue': r['venue'] or '', 'stored': ' | '.join(stored), 'ai': ' | '.join(res['ai']),
                    'first_same': 'да' if same_first else ('' if not res['ai'] else 'НЕТ'),
                    'set_same': 'да' if same_set else ('' if not res['ai'] else 'НЕТ'),
                    'model': res['model'], 'error': res['error'],
                    'description': (r['description'] or '').replace('\n', ' ')[:200], 'url': r['source_url']})
    with open(path, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(out[0].keys()) if out else ['id'])
        w.writeheader()
        w.writerows(out)
    return out


def summary(out: list[dict]) -> str:
    asked = [o for o in out if o['ai']]
    diff = [o for o in asked if o['first_same'] == 'НЕТ']
    lines = [f"Записей: {len(out)}, с ответом AI: {len(asked)}, ошибок/без ответа: {len(out) - len(asked)}",
             f"Главная категория отличается у {len(diff)} ({100 * len(diff) // max(1, len(asked))}%), "
             f"набор отличается у {sum(1 for o in asked if o['set_same'] == 'НЕТ')}"]
    moves = Counter((o['stored'].split(' | ')[0], o['ai'].split(' | ')[0]) for o in diff)
    lines.append("Чаще всего «в базе → AI»:")
    lines += [f"  {n:4d}  {a} → {b}" for (a, b), n in moves.most_common(15)]
    return "\n".join(lines)


async def main(argv=None) -> int:
    args = parse_args(argv)
    from db.connection import close_db_pool, init_db_pool  # noqa: PLC0415
    from shared.config import config  # noqa: PLC0415
    cfg = dict(config.get('event_categories') or {})
    max_chars = int(cfg.get('max_chars', 1200))
    if args.models:
        cfg['models'] = [m.strip() for m in args.models.split(',') if m.strip()]
    pool = await init_db_pool()
    try:
        rows = await fetch_rows(pool, args)
    finally:
        await close_db_pool(pool)
    groups = group_rows(rows, max_chars)
    print(f"Записей: {len(rows)}, разных по тексту (запросов к AI): {len(groups)}, источники: "
          f"{dict(Counter(r['source'] for r in rows))}")
    if args.dry_run or not rows:
        return 0
    from collector.categorizer_v2 import category_names  # noqa: PLC0415
    from shared.ai import client_from_config  # noqa: PLC0415
    client = client_from_config('event-categories', cfg)
    if not client.available:
        print("Нет ключа или моделей для AI-категорий (OPENCODE_EVENTS_API_KEY или OPENCODE_PARSING_API_KEY, models).")
        return 2
    conc = args.concurrency or int(cfg.get('concurrency', 3))
    try:
        results = await ask_all(groups, client, category_names(), max_chars, args.max_calls, conc)
    finally:
        await client.aclose()
    out = write_csv(args.out, rows, results, max_chars)
    print(summary(out))
    print(f"\nСохранено: {args.out}")
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
