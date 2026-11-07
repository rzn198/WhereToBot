"""Бенчмарк моделей для AI-дедупликации: качество и скорость на данных из БД. Ничего не меняет и не пишет в БД.

Берёт ВСЕ записи из БД (с самой ранней даты; скрытые считаются видимыми), прогоняет каждую модель отдельно
(одна модель = одна цепочка, без откатов на другие), замеряет скорость и сравнивает результаты.

    python scripts/dedup_benchmark.py --models deepseek-v4.1-flash,gpt-6-luna,glm-5.3-flash
    python scripts/dedup_benchmark.py --all-models --exclude qwen3.8-flash      # список моделей берётся со шлюза
    python scripts/dedup_benchmark.py --models ... --max-days 40                # быстрый пробный прогон
    python scripts/dedup_benchmark.py --report-only --gold <папка>/pairs_review.csv   # пересчёт по готовым результатам

Что считается (файлы в --out-dir, по умолчанию scripts/dedup_bench/<дата-время>):
    result_<модель>.json   сырой результат модели (можно прервать и продолжить: --resume пропускает готовые модели)
    summary.csv            одна строка на модель: скорость, токены, ошибки, число скрытых, согласие с остальными
    pairs_review.csv       каждая запись, которую скрыла хотя бы одна модель: кто скрыл, победитель, текст и пустая
                           колонка verdict для ручной проверки (д — настоящий дубль, н — ложная склейка)
Разметка: заполни verdict в pairs_review.csv (достаточно строк, где скрыла одна-две модели — это спорное) и запусти
    python scripts/dedup_benchmark.py --report-only --out-dir <папка> --gold <папка>/pairs_review.csv
— в summary.csv появятся ложные склейки и полнота по твоей разметке.

Качество без разметки оценивается по консенсусу: запись «в консенсусе», если её скрыли не меньше --consensus моделей
(по умолчанию половина моделей, но не меньше двух). Полнота к консенсусу — доля консенсусных записей, которые нашла
модель. «Только эта модель» — записи, которые скрыла одна модель: это кандидаты в ложные склейки ИЛИ в точные находки,
их нужно просмотреть глазами. Скорость сравнивай по задержке одного запроса (p50/p95): общее время зависит от
--concurrency и лимитов шлюза.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import os
import re
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path

if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
os.environ.setdefault('APP_ENV', 'local')

# Модели, которые уже встречались в проекте; --all-models дополняет их списком со шлюза.
KNOWN_MODELS = [
    'deepseek-v4.1-flash', 'gpt-6-luna', 'glm-5.3-flash', 'mimo-v2.6-flash', 'claude-haiku-5-5',
    'minimax-m3.1-flash', 'beam-free', 'jev-1.13', 'longcat-2.0', 'mimo-v2.6-flash-free', 'deepseek-v4.1-flash-free',
]
RETRY_PAUSES = (15, 45, 90)        # пауза перед повтором запроса, когда модель отказала (429 и т.п.)
REVIEW_TEXT = 300


# ------------------------------------------------------------------ чистая логика (покрыта тестами)

def pct(values: list[float], q: float) -> float:
    """Перцентиль q (0..1) без интерполяции; пустой список → 0."""
    if not values:
        return 0.0
    s = sorted(values)
    return s[min(len(s) - 1, max(0, math.ceil(q * len(s)) - 1))]


def default_consensus(n_models: int) -> int:
    return max(2, math.ceil(n_models / 2)) if n_models > 1 else 1


def loser_key(p: dict) -> tuple:
    return (p['loser_kind'], p['loser_id'])


def parse_verdict(value: str) -> bool | None:
    v = (value or '').strip().lower()
    if v in ('д', 'да', 'дубль', 'y', 'yes', '1', 'true'):
        return True
    if v in ('н', 'нет', 'не дубль', 'ложь', 'ложная', 'n', 'no', '0', 'false'):
        return False
    return None


def load_gold(path: Path) -> dict:
    """{(kind, id): True|False} из pairs_review.csv; строки без понятного verdict пропускаются."""
    gold = {}
    with open(path, encoding='utf-8-sig', newline='') as f:
        for r in csv.DictReader(f):
            v = parse_verdict(r.get('verdict', ''))
            if v is not None:
                gold[(r['loser_kind'], int(r['loser_id']))] = v
    return gold


def compare_models(results: dict[str, dict], consensus: int | None = None, gold: dict | None = None) -> dict:
    """results: {модель: {'pairs': [...]}}. Возвращает {'support': {ключ: [модели]}, 'rows': {модель: метрики}}."""
    models = list(results)
    thr = consensus or default_consensus(len(models))
    gold = gold or {}
    losers = {m: {loser_key(p): p for p in results[m]['pairs']} for m in models}
    support: dict[tuple, list[str]] = defaultdict(list)
    for m in models:
        for k in losers[m]:
            support[k].append(m)
    in_consensus = {k for k, ms in support.items() if len(ms) >= thr and gold.get(k) is not False}
    ref_dup = in_consensus | {k for k, v in gold.items() if v}
    rows = {}
    for m in models:
        mine = set(losers[m])
        only = {k for k in mine if len(support[k]) == 1}
        false_gold = {k for k in mine if gold.get(k) is False}
        unverified_only = {k for k in only if k not in gold}
        shared = [k for k in mine if len(support[k]) >= 2]
        diff_winner = sum(
            1 for k in shared
            if any((losers[o][k]['winner_kind'], losers[o][k]['winner_id'])
                   != (losers[m][k]['winner_kind'], losers[m][k]['winner_id'])
                   for o in support[k] if o != m))
        n = len(mine)
        rows[m] = dict(
            losers=n,
            in_consensus=len(mine & in_consensus),
            recall_consensus=round(len(mine & in_consensus) / len(in_consensus), 3) if in_consensus else None,
            only_this_model=len(only),
            only_unverified=len(unverified_only),
            false_by_gold=len(false_gold) if gold else None,
            recall_ref=round(len(mine & ref_dup) / len(ref_dup), 3) if (gold and ref_dup) else None,
            precision_low=round((n - len(false_gold) - len(unverified_only)) / n, 3) if (gold and n) else None,
            precision_high=round((n - len(false_gold)) / n, 3) if (gold and n) else None,
            diff_winner=diff_winner,
        )
    return dict(support=dict(support), rows=rows, threshold=thr, consensus_size=len(in_consensus))


def pairwise_agreement(results: dict[str, dict]) -> dict[tuple, float]:
    out = {}
    names = list(results)
    sets = {m: {loser_key(p) for p in results[m]['pairs']} for m in names}
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            union = sets[a] | sets[b]
            out[(a, b)] = round(len(sets[a] & sets[b]) / len(union), 3) if union else 1.0
    return out


def speed_stats(latencies: list[float], wall: float, tokens_out: int, requests: int) -> dict:
    return dict(
        requests=requests,
        wall_sec=round(wall, 1),
        lat_mean=round(statistics.mean(latencies), 2) if latencies else 0.0,
        lat_p50=round(pct(latencies, 0.5), 2),
        lat_p95=round(pct(latencies, 0.95), 2),
        lat_max=round(max(latencies), 2) if latencies else 0.0,
        out_tok_per_sec=round(tokens_out / sum(latencies), 1) if latencies and sum(latencies) else 0.0,
    )


# ------------------------------------------------------------------ шлюз и измерения

def safe_name(model: str) -> str:
    return re.sub(r'[^A-Za-z0-9._-]', '_', model)


async def fetch_gateway_models(base_url: str, key: str) -> list[str]:
    import httpx
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{base_url.rstrip('/')}/models", headers={'Authorization': f'Bearer {key}'})
        r.raise_for_status()
        data = r.json()
    items = data.get('data', data) if isinstance(data, dict) else data
    return sorted({(x.get('id') if isinstance(x, dict) else str(x)) for x in items if x})


async def probe_model(client_factory, model: str) -> tuple[bool, str, float]:
    """Один короткий запрос: отвечает ли модель вообще. (ok, причина, секунд)."""
    client = client_factory(model)
    t0 = time.monotonic()
    try:
        text = await client.complete('Ответь одним словом.', 'Скажи: ок')
        return bool(text), '', time.monotonic() - t0
    except Exception as e:
        return False, f"{type(e).__name__}: {e}", time.monotonic() - t0
    finally:
        await client.aclose()


def instrument(client, latencies: list[float], counters: Counter, pauses=RETRY_PAUSES):
    """Замеряет задержку каждого запроса; при отказе модели повторяет запрос после паузы (бенчмарк не должен
    обрываться на лимите шлюза), а в итоговых цифрах отказы и повторы видны отдельно."""
    from shared.ai import AIUnavailable
    original = client.complete

    async def timed(system: str, user: str) -> str:
        for attempt in range(len(pauses) + 1):
            t0 = time.monotonic()
            try:
                text = await original(system, user)
            except AIUnavailable:
                counters['failed_requests'] += 1
                if getattr(client, '_dead', False) or attempt >= len(pauses):
                    raise
                counters['retried_requests'] += 1
                await asyncio.sleep(pauses[attempt])
                client._idx = 0                      # снова к единственной модели; статистику не сбрасываем
                client._bad_in_row = 0
                continue
            latencies.append(time.monotonic() - t0)
            return text
        raise AssertionError('unreachable')

    client.complete = timed
    return client


def plans_to_pairs(plans, resolver) -> list[dict]:
    rows = []
    for plan in plans:
        for nd in plan.wanted:
            rows.append(dict(
                day=plan.day.isoformat(), via=nd.via, reason=nd.reason,
                loser_kind=nd.loser.kind, loser_id=nd.loser.id, loser_src=resolver.label(nd.loser),
                loser_text=' '.join((nd.loser.text or '').split())[:REVIEW_TEXT],
                winner_kind=nd.winner.kind, winner_id=nd.winner.id, winner_src=resolver.label(nd.winner),
                winner_text=' '.join((nd.winner.text or '').split())[:REVIEW_TEXT]))
    return rows


def plans_extra(plans) -> dict:
    sizes = [len(g.members) for p in plans for g in p.groups]
    return dict(groups=len(sizes), big_groups=sum(1 for s in sizes if s > 8),
                lists=sum(len(p.lists) for p in plans), weak=sum(len(p.weak) for p in plans))


# ------------------------------------------------------------------ отчёты

SUMMARY_FIELDS = [
    'model', 'status', 'dates', 'requests', 'failed_requests', 'retried_requests', 'bad_days', 'salvaged',
    'wall_sec', 'lat_mean', 'lat_p50', 'lat_p95', 'lat_max', 'tokens_in', 'tokens_out', 'out_tok_per_sec',
    'groups', 'big_groups', 'losers', 'in_consensus', 'recall_consensus', 'only_this_model', 'only_unverified',
    'false_by_gold', 'recall_ref', 'precision_low', 'precision_high', 'diff_winner', 'lists', 'weak',
]
REVIEW_FIELDS = ['support', 'models', 'day', 'loser_kind', 'loser_id', 'loser_src', 'loser_text', 'winner_src',
                 'winner_text', 'reason', 'verdict']


def build_summary(results: dict[str, dict], consensus: int | None, gold: dict | None) -> tuple[list[dict], dict]:
    ok = {m: r for m, r in results.items() if r.get('status') == 'ok'}
    cmp = compare_models(ok, consensus, gold) if ok else dict(support={}, rows={}, threshold=0, consensus_size=0)
    rows = []
    for m, r in results.items():
        row = dict(model=m, status=r.get('status'), **{k: r.get(k) for k in (
            'dates', 'requests', 'failed_requests', 'retried_requests', 'bad_days', 'salvaged', 'wall_sec',
            'lat_mean', 'lat_p50', 'lat_p95', 'lat_max', 'tokens_in', 'tokens_out', 'out_tok_per_sec',
            'groups', 'big_groups', 'lists', 'weak')})
        row.update(cmp['rows'].get(m, {}))
        rows.append(row)
    return rows, cmp


def build_review(results: dict[str, dict], cmp: dict, gold: dict | None) -> list[dict]:
    gold = gold or {}
    best: dict[tuple, dict] = {}
    for m, r in results.items():
        if r.get('status') != 'ok':
            continue
        for p in r['pairs']:
            best.setdefault(loser_key(p), p)
    out = []
    for k, p in best.items():
        ms = cmp['support'].get(k, [])
        v = gold.get(k)
        out.append(dict(support=len(ms), models=', '.join(ms), day=p['day'], loser_kind=k[0], loser_id=k[1],
                        loser_src=p['loser_src'], loser_text=p['loser_text'], winner_src=p['winner_src'],
                        winner_text=p['winner_text'], reason=p['reason'],
                        verdict='' if v is None else ('д' if v else 'н')))
    out.sort(key=lambda r: (r['support'], r['day'], str(r['loser_id'])))          # спорное (меньше всего голосов) первым
    return out


def write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    with open(path, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)


def print_summary(rows: list[dict], cmp: dict, agreement: dict) -> None:
    print("\n=== Итоги по моделям ===")
    print(f"{'модель':26} {'статус':8} {'wall,с':>7} {'p50':>6} {'p95':>6} {'ток.out':>8} {'скрыто':>6} "
          f"{'консенс.':>8} {'полнота':>7} {'только эта':>10} {'ошибок':>6}")
    for r in rows:
        print(f"{str(r['model']):26} {str(r.get('status')):8} {r.get('wall_sec') or 0:>7} {r.get('lat_p50') or 0:>6} "
              f"{r.get('lat_p95') or 0:>6} {r.get('tokens_out') or 0:>8} {r.get('losers') or 0:>6} "
              f"{r.get('in_consensus') or 0:>8} {str(r.get('recall_consensus') if r.get('recall_consensus') is not None else '-'):>7} "
              f"{r.get('only_this_model') or 0:>10} {(r.get('failed_requests') or 0) + (r.get('bad_days') or 0):>6}")
    if cmp.get('threshold'):
        print(f"\nКонсенсус: запись скрыли ≥{cmp['threshold']} моделей; таких записей {cmp['consensus_size']}.")
    if agreement:
        print("\n=== Согласие пар моделей (доля общих скрытых записей) ===")
        for (a, b), v in sorted(agreement.items(), key=lambda x: -x[1]):
            print(f"  {a} ↔ {b}: {v:.0%}")


def build_report(out_dir: Path, consensus: int | None, gold_path: Path | None) -> None:
    results = {}
    for p in sorted(out_dir.glob('result_*.json')):
        r = json.loads(p.read_text(encoding='utf-8'))
        results[r['model']] = r
    if not results:
        sys.exit(f'В {out_dir} нет result_*.json')
    gold = load_gold(gold_path) if gold_path else None
    rows, cmp = build_summary(results, consensus, gold)
    ok = {m: r for m, r in results.items() if r.get('status') == 'ok'}
    write_csv(out_dir / 'summary.csv', SUMMARY_FIELDS, rows)
    write_csv(out_dir / 'pairs_review.csv', REVIEW_FIELDS, build_review(results, cmp, gold))
    print_summary(rows, cmp, pairwise_agreement(ok) if len(ok) > 1 else {})
    if gold:
        print(f"\nРазметка: {len(gold)} записей (дубль: {sum(gold.values())}, не дубль: {len(gold) - sum(gold.values())}).")
    print(f"\n→ {out_dir / 'summary.csv'}\n→ {out_dir / 'pairs_review.csv'}  (заполни колонку verdict: д / н)")


# ------------------------------------------------------------------ запуск

async def run_model(pool, model: str, ai: dict, key: str, args, start: date) -> dict:
    from loguru import logger
    from deduplicator import runner
    from shared.ai import client_from_config

    latencies: list[float] = []
    counters: Counter = Counter()
    seen: Counter = Counter()

    def sink(message):
        text = message.record['message']
        if 'DEDUP_JSON_SALVAGED' in text:
            seen['salvaged'] += 1

    sink_id = logger.add(sink, level='WARNING')
    client = client_from_config('dedup', {**ai, 'api_key': key, 'models': [model], 'timeout': args.timeout})
    instrument(client, latencies, counters)
    run_ai = {**ai, 'concurrency': args.concurrency, 'horizon_days': 3650,
              'max_requests_per_run': 100000, 'max_requests_per_day': args.max_requests_per_day}
    if args.max_records:
        run_ai['max_records_per_request'] = args.max_records
    t0 = time.monotonic()
    try:
        res = await runner.run_dedup(pool, client, {'ai': run_ai}, method='shadow', dry_run=True, force=True,
                                     start_date=start, ignore_hidden=not args.respect_hidden,
                                     max_days=args.max_days or None, ignore_decisions=True)
    finally:
        wall = time.monotonic() - t0
        await client.aclose()
        logger.remove(sink_id)
    st = res.stats
    out = dict(model=model, status=res.status, error=res.error, dates=st['dates_processed'],
               dates_total=st['dates_total'], bad_days=st['bad_days'], skipped_days=st['skipped_days'],
               tokens_in=st['tokens_in'], tokens_out=st['tokens_out'], salvaged=seen['salvaged'],
               failed_requests=counters['failed_requests'], retried_requests=counters['retried_requests'],
               **speed_stats(latencies, wall, st['tokens_out'], st['requests']))
    if res.status == 'ok' and res.resolver is not None:
        out['pairs'] = plans_to_pairs(res.plans, res.resolver)
        out.update(plans_extra(res.plans))
        out['losers'] = len(out['pairs'])
    return out


async def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--models', help='через запятую')
    ap.add_argument('--all-models', action='store_true', help='известные модели проекта + список со шлюза (/models)')
    ap.add_argument('--exclude', default='', help='модели, которые не надо брать (через запятую)')
    ap.add_argument('--from', dest='start', help='начальная дата ГГГГ-ММ-ДД (по умолчанию — самая ранняя запись в БД)')
    ap.add_argument('--max-days', type=int, default=0, help='обработать не больше N дат (0 — все)')
    ap.add_argument('--concurrency', type=int, default=2)
    ap.add_argument('--timeout', type=float, default=180)
    ap.add_argument('--max-records', type=int, default=0, help='записей в одном запросе (по умолчанию из конфига)')
    ap.add_argument('--max-requests-per-day', type=int, default=21)
    ap.add_argument('--respect-hidden', action='store_true')
    ap.add_argument('--use-parsing-key', action='store_true')
    ap.add_argument('--consensus', type=int, default=0, help='сколько моделей должны скрыть запись, чтобы она стала консенсусной')
    ap.add_argument('--gold', help='pairs_review.csv с заполненной колонкой verdict')
    ap.add_argument('--resume', action='store_true', help='пропустить модели, у которых уже есть result_*.json')
    ap.add_argument('--no-probe', action='store_true', help='не проверять доступность моделей коротким запросом')
    ap.add_argument('--report-only', action='store_true', help='только пересчитать отчёт по готовым result_*.json')
    ap.add_argument('--out-dir')
    args = ap.parse_args()

    out_dir = Path(args.out_dir) if args.out_dir else HERE / 'dedup_bench' / datetime.now().strftime('%Y%m%d_%H%M')
    out_dir.mkdir(parents=True, exist_ok=True)
    gold_path = Path(args.gold) if args.gold else None
    if args.report_only:
        build_report(out_dir, args.consensus or None, gold_path)
        return

    from shared.config import config
    from db.connection import init_db_pool, close_db_pool
    from db.tables import Tables
    from loguru import logger
    from shared.ai import client_from_config
    logger.remove()
    logger.add(sys.stderr, level='ERROR')

    ai = dict(config['deduplication']['ai'])
    key = ai.get('api_key') or (config['parsing'].get('api_key') if args.use_parsing_key else '')
    if not key:
        sys.exit('Нет OPENCODE_DEDUP_API_KEY (или добавь --use-parsing-key)')

    excluded = {m.strip() for m in args.exclude.split(',') if m.strip()}
    if args.models:
        models = [m.strip() for m in args.models.split(',') if m.strip()]
    elif args.all_models:
        models = list(KNOWN_MODELS)
        try:
            found = await fetch_gateway_models(ai['base_url'], key)
            print(f"Шлюз вернул {len(found)} моделей.")
            models += [m for m in found if m not in models]
        except Exception as e:
            print(f"Список моделей со шлюза получить не удалось ({type(e).__name__}: {e}); беру известные модели проекта.")
    else:
        sys.exit('Укажи --models a,b,c или --all-models')
    models = [m for m in dict.fromkeys(models) if m not in excluded]

    def factory(model):
        return client_from_config('dedup', {**ai, 'api_key': key, 'models': [model], 'timeout': 60})

    if not args.no_probe:
        print(f"Проверяю доступность {len(models)} моделей...")
        alive = []
        for m in models:
            ok, why, sec = await probe_model(factory, m)
            print(f"  {'ok ' if ok else 'нет'} {m} ({sec:.1f} с){'' if ok else ' — ' + why[:80]}")
            if ok:
                alive.append(m)
        models = alive
    if not models:
        sys.exit('Нет доступных моделей')

    pool = await init_db_pool()
    try:
        if args.start:
            start = date.fromisoformat(args.start)
        else:
            start = await pool.fetchval(
                f"SELECT LEAST((SELECT min(date) FROM {Tables.MESSAGES}), (SELECT min(starts_at) FROM {Tables.EVENTS}))")
            start = start or date.today()
        print(f"\nДанные с {start}; моделей: {len(models)}; результаты → {out_dir}\n")
        for i, model in enumerate(models, 1):
            path = out_dir / f"result_{safe_name(model)}.json"
            if args.resume and path.exists():
                print(f"[{i}/{len(models)}] {model}: уже есть результат, пропускаю")
                continue
            print(f"[{i}/{len(models)}] {model}: считаю...", flush=True)
            res = await run_model(pool, model, ai, key, args, start)
            path.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding='utf-8')
            print(f"    {res['status']} · дат {res['dates']}/{res.get('dates_total')} · запросов {res['requests']} · "
                  f"{res['wall_sec']} с · p50 {res['lat_p50']} с · p95 {res['lat_p95']} с · "
                  f"токены {res['tokens_in']}/{res['tokens_out']} · скрыто {res.get('losers', '-')} · "
                  f"плохих дат {res['bad_days']} · отказов {res['failed_requests']}"
                  + (f" · {res['error']}" if res.get('error') else ''), flush=True)
    finally:
        await close_db_pool(pool)
    build_report(out_dir, args.consensus or None, gold_path)


if __name__ == '__main__':
    asyncio.run(main())
