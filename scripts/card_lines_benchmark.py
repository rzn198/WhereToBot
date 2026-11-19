"""Сравнение моделей для ИИ-строк списка: одни и те же записи через каждую модель (и размер пакета), результат в CSV.
В БД ничего не пишется (только чтение записей), ИИ-строки не создаются.

    docker compose exec deduplicator python scripts/card_lines_benchmark.py --per-profile 8
    docker compose exec deduplicator python scripts/card_lines_benchmark.py --models glm-5.3-flash,gpt-6-luna --batch-sizes 1,5,10 --per-profile 10
    docker compose exec deduplicator python scripts/card_lines_benchmark.py --profiles "Концерты,Кино/Театр" --out /app/logs/cl.csv

--models        список через запятую (по умолчанию card_lines.models из config.yaml)
--batch-sizes   размеры пакета через запятую (по умолчанию 1); пакет берётся из записей одного профиля
--per-profile   записей каждого профиля в выборке (по умолчанию 8; события и посты вперемешку)
--profiles      только эти категории (по умолчанию все профили с ИИ)
--seed          зерно случайной выборки: одинаковая выборка у всех моделей и запусков
Ключ: OPENCODE_CARDLINES_API_KEY.
CSV: model, batch_size, profile, kind, id, source_title, source_text, title, value, status, error, latency_ms (задержка запроса,
у пакета одна на все записи), call_size, tokens_in, tokens_out (по запросу). В конце — сводка по каждой паре модель/пакет.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import os
import random
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('APP_ENV', 'production')

from card_lines.select import select_candidates  # noqa: E402
from card_lines.service import DEFAULT_CITY, config_horizon_days, generate, make_chunks  # noqa: E402
from card_lines.settings import card_line_settings  # noqa: E402
from card_lines.text import BadCardLine  # noqa: E402
from db.connection import close_db_pool, init_db_pool  # noqa: E402
from shared.ai import AIClient, AIUnavailable  # noqa: E402
from shared.config import config  # noqa: E402
from shared.tz import belgrade_today  # noqa: E402

FIELDS = ['model', 'batch_size', 'profile', 'kind', 'id', 'source_title', 'source_text', 'title', 'value', 'status', 'error',
          'latency_ms', 'call_size', 'tokens_in', 'tokens_out']


def sample(cands, per_profile: int, profiles: set[str] | None, seed: int):
    rng = random.Random(seed)
    by = defaultdict(list)
    for c in cands:
        if not profiles or c.profile.name in profiles:
            by[c.profile.name].append(c)
    out = []
    for name in sorted(by):
        group = by[name]
        out += rng.sample(group, min(per_profile, len(group)))
    return out


async def run_one(model: str, batch: int, cands, st, base_cfg: dict, concurrency: int):
    client = AIClient('card-lines-bench', base_cfg['api_key'], [model], base_url=base_cfg['base_url'],
                      timeout=float(base_cfg.get('timeout') or 60), temperature=base_cfg.get('temperature'))
    client.begin_run()
    chunks = make_chunks(cands, batch)
    sem = asyncio.Semaphore(concurrency)
    rows = []

    async def one(chunk):
        async with sem:
            before_in, before_out = client.stats.prompt_tokens, client.stats.completion_tokens
            t0 = time.perf_counter()
            try:
                parts = await generate(client, chunk, st)
                err = ''
            except AIUnavailable as e:
                parts, err = [BadCardLine(f'недоступна: {e}')] * len(chunk), str(e)
            ms = (time.perf_counter() - t0) * 1000
            tin, tout = client.stats.prompt_tokens - before_in, client.stats.completion_tokens - before_out
        for c, p in zip(chunk, parts):
            bad = isinstance(p, BadCardLine)
            rows.append({
                'model': model, 'batch_size': batch, 'profile': c.profile.name, 'kind': c.kind, 'id': c.id,
                'source_title': c.title, 'source_text': (c.body or '').replace('\n', ' ')[:200],
                'title': '' if bad else p.title, 'value': '' if bad else (p.value or ''),
                'status': 'bad' if bad else ('ok' if p.value else 'ok_no_value'), 'error': str(p) if bad else err,
                'latency_ms': f'{ms:.0f}', 'call_size': len(chunk), 'tokens_in': tin, 'tokens_out': tout,
            })

    await asyncio.gather(*[one(c) for c in chunks])
    await client.aclose()
    return rows


def summary(rows) -> str:
    by = defaultdict(list)
    for r in rows:
        by[(r['model'], r['batch_size'])].append(r)
    lines = [f"{'модель':28} {'пакет':>5} {'записей':>7} {'валидно':>8} {'со знач.':>8} {'мс/запрос':>9} {'токен.вх/зап':>12} {'токен.вых/зап':>13}"]
    for (model, batch), rs in sorted(by.items()):
        n_calls = max(1, round(sum(1 / r['call_size'] for r in rs)))
        valid = sum(r['status'] != 'bad' for r in rs)
        with_val = sum(r['status'] == 'ok' for r in rs)
        lat = sum(float(r['latency_ms']) / r['call_size'] for r in rs) / n_calls
        tin = sum(r['tokens_in'] / r['call_size'] for r in rs) / n_calls
        tout = sum(r['tokens_out'] / r['call_size'] for r in rs) / n_calls
        lines.append(f"{model:28} {batch:>5} {len(rs):>7} {valid / len(rs):>8.0%} {with_val / len(rs):>8.0%} {lat:>9.0f} {tin:>12.0f} {tout:>13.0f}")
    return "\n".join(lines)


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--models', default='')
    ap.add_argument('--batch-sizes', default='1')
    ap.add_argument('--per-profile', type=int, default=8)
    ap.add_argument('--profiles', default='')
    ap.add_argument('--seed', type=int, default=1)
    ap.add_argument('--concurrency', type=int, default=1, help='параллельных запросов (1 = чистые замеры задержки)')
    ap.add_argument('--out', default='/tmp/card_lines_benchmark.csv')
    args = ap.parse_args()

    cfg = config['card_lines']
    models = [m.strip() for m in (args.models or ','.join(cfg.get('models') or [])).split(',') if m.strip()]
    batches = [int(b) for b in args.batch_sizes.split(',') if b.strip()]
    if not models:
        sys.exit('Нет моделей: укажи --models или card_lines.models в config.yaml')
    if not cfg.get('api_key'):
        sys.exit('Нет ключа: задай OPENCODE_CARDLINES_API_KEY в .env')
    st = card_line_settings({**cfg, 'method': 'on', 'after_dedup': False})

    pool = await init_db_pool()
    try:
        sel = await select_candidates(pool, st, today=belgrade_today(), horizon_days=config_horizon_days(),
                                      city=DEFAULT_CITY, include_done=True)
    finally:
        await close_db_pool(pool)
    profiles = {p.strip() for p in args.profiles.split(',') if p.strip()}
    cands = sample(sel.todo, args.per_profile, profiles, args.seed)
    print(f'Выборка: {len(cands)} записей из {len(sel.todo)} (профили: {sorted({c.profile.name for c in cands})})')

    all_rows = []
    for model in models:
        for batch in batches:
            print(f'→ {model}, пакет {batch} ...', flush=True)
            all_rows += await run_one(model, batch, cands, st, cfg, args.concurrency)
    with open(args.out, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(all_rows)
    print(f'\nCSV: {args.out}\n')
    print(summary(all_rows))


if __name__ == '__main__':
    asyncio.run(main())
