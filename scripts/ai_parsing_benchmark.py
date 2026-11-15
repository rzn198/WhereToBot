"""Бенчмарк моделей для AI-парсинга дат и категорий (collector): одни и те же посты из БД через каждую модель
ОТДЕЛЬНО (одна модель = одна цепочка, без откатов на другие). В БД ничего не пишет.

    python scripts/ai_parsing_benchmark.py                                   # модели по умолчанию, 600 свежих постов
    python scripts/ai_parsing_benchmark.py --models gpt-5.6-luna,beam-free --limit 300
    python scripts/ai_parsing_benchmark.py --limit 1000 --random --seed 7    # случайная выборка (повторяема по seed)
    python scripts/ai_parsing_benchmark.py --resume --out-dir scripts/ai_parsing_bench/run1   # продолжить после обрыва
    python scripts/ai_parsing_benchmark.py --report-only --out-dir <папка>                    # пересобрать отчёт из готового

Нужны: OPENCODE_PARSING_API_KEY в .env.local (или .env) и доступ к БД с постами (APP_ENV=local по умолчанию).
`parsing.models` из config.yaml НЕ используется: модели берутся из --models.

Что получится в --out-dir (по умолчанию scripts/ai_parsing_bench/<дата-время>):
    sample.csv                 выборка постов (id, канал, дата публикации, что лежит в БД, текст) — одна для всех моделей
    result_<модель>.csv        ответ этой модели по каждому посту: дата, категории, ошибка, задержка
    result_<модель>.json       итог по модели: скорость, токены, ошибки, сравнение с БД и консенсусом
    summary.csv                одна строка на модель — для сравнения
    comparison.csv             одна строка на пост: БД и ответы всех моделей рядом + пустые gold_date / gold_cats
    disagreements.csv          только посты, где модели разошлись по дате (самое полезное для разбора)
    report.txt                 то же, что печатается в консоль

Разметка (по желанию): в comparison.csv заполни gold_date (ГГГГ-ММ-ДД или «нет») и gold_cats (через «;») у спорных
строк и запусти с --report-only --gold <comparison.csv> — появится точность каждой модели и старого метода (БД).

Консенсус по дате: дата, которую назвали не меньше половины ответивших моделей (минимум две). Консенсус — не истина:
он показывает согласие моделей, а не правоту. Посты, где модель одна против остальных, нужно смотреть глазами.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import os
import random
import statistics
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault('APP_ENV', 'local')

DEFAULT_MODELS = ['gpt-5.6-luna', 'mimo-v2.6-flash', 'minimax-m3.1-flash', 'beam-free', 'glm-5.3-flash']
RETRY_PAUSES = (10, 30, 60)           # пауза перед повтором при отказе шлюза (429 и т.п.), секунд
TEXT_IN_SAMPLE = 700                  # символов текста в sample.csv / comparison.csv
SNIPPET = 160


# ------------------------------------------------------------------ чистые функции (без сети и БД)

def pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    return s[min(len(s) - 1, max(0, math.ceil(q * len(s)) - 1))]


def safe_name(model: str) -> str:
    return ''.join(c if c.isalnum() or c in '.-_' else '_' for c in model)


def split_cats(value: str) -> list[str]:
    return [c.strip() for c in (value or '').split(';') if c.strip()]


def consensus_threshold(n_answered: int) -> int:
    return max(2, math.ceil(n_answered / 2)) if n_answered > 1 else 99      # одна модель консенсуса не даёт


def consensus_date(dates: list[str]) -> str | None:
    """Дата ('' = «даты нет»), которую назвали не меньше половины ответивших моделей. None — консенсуса нет."""
    answered = list(dates)
    if len(answered) < 2:
        return None
    value, cnt = Counter(answered).most_common(1)[0]
    return value if cnt >= consensus_threshold(len(answered)) else None


def consensus_cats(cat_lists: list[list[str]]) -> list[str] | None:
    """Категории, которые назвали не меньше половины ответивших моделей. None — консенсуса нет (нет ответов)."""
    if len(cat_lists) < 2:
        return None
    need = consensus_threshold(len(cat_lists))
    votes = Counter(c for cats in cat_lists for c in set(cats))
    return sorted(c for c, n in votes.items() if n >= need)


def summarize_model(rows: list[dict], by_id_all: dict[int, dict], model: str) -> dict:
    """Метрики одной модели по готовым строкам результата. by_id_all: id → {модель: строка} (для консенсуса)."""
    ok = [r for r in rows if not r['error']]
    errs = Counter()
    for r in rows:
        if r['error']:
            errs['bad_output' if r['error'].startswith('bad_output') else 'unavailable'] += 1
    past = sum(1 for r in rows if r['error'].startswith('bad_output: дата в прошлом'))
    date_vs_db = Counter()
    cats_exact = 0
    cats_overlap = 0.0
    vs_cons_n = vs_cons_ok = alone = cats_cons_n = cats_cons_ok = 0
    non_event = 0
    for r in ok:
        db_d, ai_d = r['stored_date'], r['ai_date']
        if ai_d and not db_d:
            date_vs_db['ai_found_db_none'] += 1
        elif db_d and not ai_d:
            date_vs_db['db_found_ai_none'] += 1
        elif ai_d and db_d and ai_d != db_d:
            date_vs_db['both_differ'] += 1
        else:
            date_vs_db['match'] += 1
        a, s = set(split_cats(r['ai_cats'])), set(split_cats(r['stored_cats']))
        cats_exact += int(a == s)
        cats_overlap += (len(a & s) / len(a | s)) if (a | s) else 1.0
        non_event += int(not ai_d and not a)
        others = [o for m, o in by_id_all.get(r['id'], {}).items() if m != model and not o['error']]
        everyone = [r] + others
        cd = consensus_date([o['ai_date'] for o in everyone])
        if cd is not None:
            vs_cons_n += 1
            vs_cons_ok += int(ai_d == cd)
        if others and all(o['ai_date'] != ai_d for o in others) and ai_d:
            alone += 1
        cc = consensus_cats([split_cats(o['ai_cats']) for o in everyone])
        if cc is not None:
            cats_cons_n += 1
            cats_cons_ok += int(set(cc) == a)
    lat = [float(r['latency']) for r in ok if r['latency'] not in ('', None)]
    n = len(ok)
    return dict(
        model=model, posts=len(rows), answered=n, bad_output=errs['bad_output'], unavailable=errs['unavailable'],
        year_guard=past, non_event=non_event,
        date_match_db=date_vs_db['match'], date_match_db_pct=round(100 * date_vs_db['match'] / n, 1) if n else 0.0,
        ai_found_db_none=date_vs_db['ai_found_db_none'], db_found_ai_none=date_vs_db['db_found_ai_none'],
        both_differ=date_vs_db['both_differ'],
        date_vs_consensus_n=vs_cons_n, date_vs_consensus_pct=round(100 * vs_cons_ok / vs_cons_n, 1) if vs_cons_n else 0.0,
        date_alone=alone,
        cats_exact_db_pct=round(100 * cats_exact / n, 1) if n else 0.0,
        cats_overlap_db_pct=round(100 * cats_overlap / n, 1) if n else 0.0,
        cats_vs_consensus_pct=round(100 * cats_cons_ok / cats_cons_n, 1) if cats_cons_n else 0.0,
        lat_mean=round(statistics.mean(lat), 2) if lat else 0.0, lat_p50=round(pct(lat, .5), 2),
        lat_p95=round(pct(lat, .95), 2), lat_max=round(max(lat), 2) if lat else 0.0,
    )


def gold_scores(comparison: list[dict], models: list[str]) -> dict[str, dict]:
    """Точность по размеченным строкам. gold_date: ГГГГ-ММ-ДД или «нет»; gold_cats: через «;» (пусто = не размечено)."""
    out = {}
    for who in ['db'] + models:
        dn = dok = cn = cok = 0
        for r in comparison:
            gd = (r.get('gold_date') or '').strip().lower()
            gc = (r.get('gold_cats') or '').strip()
            if who == 'db':
                got_d, got_c = r['stored_date'], r['stored_cats']
                answered = True
            else:
                got_d, got_c = r.get(f'{who}__date', ''), r.get(f'{who}__cats', '')
                answered = (r.get(f'{who}__error', '') == '') and f'{who}__date' in r
            if gd and answered:
                dn += 1
                want = '' if gd in ('нет', 'none', 'null', '-') else gd
                dok += int(got_d == want)
            if gc and answered:
                cn += 1
                want_c = set() if gc.lower() in ('нет', 'none', '-') else set(split_cats(gc))
                cok += int(set(split_cats(got_c)) == want_c)
        out[who] = dict(gold_dates=dn, gold_date_ok=dok, gold_date_pct=round(100 * dok / dn, 1) if dn else 0.0,
                        gold_cats=cn, gold_cats_ok=cok, gold_cats_pct=round(100 * cok / cn, 1) if cn else 0.0)
    return out


def build_comparison(sample: list[dict], results: dict[str, list[dict]], channel_names: dict[int, str]) -> list[dict]:
    by_model = {m: {r['id']: r for r in rows} for m, rows in results.items()}
    out = []
    for s in sample:
        row = dict(id=s['id'], channel=channel_names.get(s['channel_id'], str(s['channel_id'])),
                   published=s['published'], stored_date=s['stored_date'], stored_cats=s['stored_cats'],
                   text=s['text'][:TEXT_IN_SAMPLE])
        dates_ans = []
        for m, rows in by_model.items():
            r = rows.get(s['id'])
            if r is None:
                continue
            row[f'{m}__date'] = r['ai_date']
            row[f'{m}__cats'] = r['ai_cats']
            row[f'{m}__error'] = r['error'][:80]
            if not r['error']:
                dates_ans.append(r['ai_date'])
        cd = consensus_date(dates_ans)
        row['consensus_date'] = '' if cd is None else (cd or 'нет')
        distinct = set(dates_ans)
        row['models_disagree'] = int(len(distinct) > 1)
        row['db_differs_from_consensus'] = int(cd is not None and cd != (s['stored_date'] or ''))
        row['gold_date'] = ''
        row['gold_cats'] = ''
        out.append(row)
    return out


def format_summary(rows: list[dict], gold: dict[str, dict] | None = None) -> str:
    head = (f"{'модель':26} {'ответов':>8} {'ошибок':>7} {'дата=БД%':>9} {'дата=конс%':>11} {'одна':>5} "
            f"{'кат=БД%':>8} {'кат=конс%':>10} {'p50,с':>6} {'p95,с':>6}")
    lines = [head]
    for r in rows:
        lines.append(f"{r['model']:26} {r['answered']:>8} {r['bad_output'] + r['unavailable']:>7} "
                     f"{r['date_match_db_pct']:>9} {r['date_vs_consensus_pct']:>11} {r['date_alone']:>5} "
                     f"{r['cats_exact_db_pct']:>8} {r['cats_vs_consensus_pct']:>10} {r['lat_p50']:>6} {r['lat_p95']:>6}")
    if gold:
        lines.append("\nПо вашей разметке (gold):")
        for who, g in gold.items():
            if g['gold_dates'] or g['gold_cats']:
                lines.append(f"  {('БД (regex+словарь)' if who == 'db' else who):26} даты {g['gold_date_ok']}/{g['gold_dates']} "
                             f"({g['gold_date_pct']}%) | категории {g['gold_cats_ok']}/{g['gold_cats']} ({g['gold_cats_pct']}%)")
    return '\n'.join(lines)


# ------------------------------------------------------------------ запросы к модели

async def ask_with_retries(client, extractor, text, published, allowed, max_chars, counters, pauses=RETRY_PAUSES):
    """Один пост через одну модель. Отказ шлюза — пауза и повтор (бенчмарк не должен обрываться на лимите)."""
    from shared.ai import AIUnavailable
    for attempt in range(len(pauses) + 1):
        t0 = time.monotonic()
        try:
            res = await extractor.extract(client, text, published, allowed, max_chars)
            return res, time.monotonic() - t0, ''
        except extractor.BadAIOutput as e:
            return None, time.monotonic() - t0, f'bad_output: {e}'
        except AIUnavailable as e:
            counters['failed_requests'] += 1
            if getattr(client, '_dead', False) or attempt >= len(pauses):
                return None, 0.0, f'unavailable: {e}'
            counters['retried_requests'] += 1
            await asyncio.sleep(pauses[attempt])
            client._idx = 0                  # снова к единственной модели
            client._bad_in_row = 0
        except Exception as e:               # неожиданное — не роняем весь прогон
            return None, time.monotonic() - t0, f'{type(e).__name__}: {e}'
    return None, 0.0, 'unavailable'


async def run_model(model: str, sample: list[dict], cfg: dict, args, extractor, allowed, client_factory) -> tuple[list[dict], dict]:
    client = client_factory(model)
    counters: Counter = Counter()
    sem = asyncio.Semaphore(args.concurrency)
    rows: list[dict] = []
    done = 0
    t0 = time.monotonic()

    async def one(s):
        nonlocal done
        async with sem:
            res, lat, err = await ask_with_retries(client, extractor, s['text_full'], s['published_date'], allowed,
                                                   args.max_chars or cfg['max_chars'], counters)
        rows.append(dict(id=s['id'], ai_date=(res.date.isoformat() if res and res.date else ''),
                         ai_cats='; '.join(res.categories) if res else '', error=err,
                         latency=round(lat, 2) if not err else '',
                         stored_date=s['stored_date'], stored_cats=s['stored_cats']))
        done += 1
        if done % 50 == 0:
            print(f"    {model}: {done}/{len(sample)}", flush=True)

    try:
        await asyncio.gather(*[one(s) for s in sample])
    finally:
        wall = time.monotonic() - t0
        stats = client.stats
        await client.aclose()
    rows.sort(key=lambda r: r['id'])
    meta = dict(model=model, wall_sec=round(wall, 1), tokens_in=stats.prompt_tokens, tokens_out=stats.completion_tokens,
                failed_requests=counters['failed_requests'], retried_requests=counters['retried_requests'])
    return rows, meta


# ------------------------------------------------------------------ выборка и файлы

SAMPLE_FIELDS = ['id', 'channel_id', 'published', 'stored_date', 'stored_cats', 'text']
RESULT_FIELDS = ['id', 'ai_date', 'ai_cats', 'error', 'latency', 'stored_date', 'stored_cats']


def write_csv(path: Path, rows: list[dict], fields: list[str] | None = None):
    fields = fields or (list(rows[0].keys()) if rows else ['id'])
    with open(path, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)


def read_csv(path: Path) -> list[dict]:
    with open(path, newline='', encoding='utf-8-sig') as f:
        return list(csv.DictReader(f))


async def load_sample(pool, args, out_dir: Path) -> list[dict]:
    ids_file = out_dir / 'sample_ids.json'
    if ids_file.exists() and args.resume:
        ids = json.loads(ids_file.read_text(encoding='utf-8'))
        rows = await pool.fetch(
            "SELECT surrogate_id, channel_id, text, date, published_at, event_types FROM messages "
            "WHERE surrogate_id = ANY($1::bigint[])", ids)
    else:
        where, a = ["text <> ''", "position = 0"], []
        if args.days:
            a.append(args.days)
            where.append(f"published_at >= now() - make_interval(days => ${len(a)}::int)")
        if args.channel:
            a.append(args.channel)
            where.append(f"channel_id = ${len(a)}")
        base = (f"SELECT surrogate_id, channel_id, text, date, published_at, event_types FROM messages "
                f"WHERE {' AND '.join(where)}")
        if args.random:
            rows = list(await pool.fetch(base, *a))
            random.Random(args.seed).shuffle(rows)
            rows = rows[:args.limit]
        else:
            a.append(args.limit)
            rows = await pool.fetch(f"{base} ORDER BY published_at DESC LIMIT ${len(a)}", *a)
        ids_file.write_text(json.dumps([r['surrogate_id'] for r in rows]), encoding='utf-8')
    sample = []
    for r in sorted(rows, key=lambda r: r['surrogate_id']):
        flat = ' '.join(r['text'].split())
        sample.append(dict(
            id=r['surrogate_id'], channel_id=r['channel_id'], published=r['published_at'].date().isoformat(),
            published_date=r['published_at'].date(), stored_date=r['date'].isoformat() if r['date'] else '',
            stored_cats='; '.join(r['event_types'] or []), text=flat, text_full=r['text']))
    return sample


async def channel_names(pool) -> dict[int, str]:
    try:
        rows = await pool.fetch("SELECT channel_id, username, title FROM channels")
    except Exception:
        return {}
    return {r['channel_id']: (f"@{r['username']}" if r['username'] else (r['title'] or str(r['channel_id']))) for r in rows}


def build_report(out_dir: Path, models: list[str], sample: list[dict], gold_path: str | None, names: dict[int, str]):
    results, metas = {}, {}
    for m in models:
        p, j = out_dir / f'result_{safe_name(m)}.csv', out_dir / f'result_{safe_name(m)}.json'
        if p.exists():
            rows = read_csv(p)
            for r in rows:
                r['id'] = int(r['id'])
            results[m] = rows
            metas[m] = json.loads(j.read_text(encoding='utf-8')) if j.exists() else {}
    if not results:
        print("Нет готовых результатов моделей в", out_dir)
        return
    by_id: dict[int, dict] = {}
    for m, rows in results.items():
        for r in rows:
            by_id.setdefault(r['id'], {})[m] = r
    summary = []
    for m, rows in results.items():
        s = summarize_model(rows, by_id, m)
        s.update({k: v for k, v in metas[m].items() if k in ('wall_sec', 'tokens_in', 'tokens_out', 'failed_requests', 'retried_requests')})
        summary.append(s)
        (out_dir / f'result_{safe_name(m)}.json').write_text(json.dumps({**metas[m], **s}, ensure_ascii=False, indent=1), encoding='utf-8')
    write_csv(out_dir / 'summary.csv', summary)
    cmp_rows = build_comparison(sample, results, names)
    # Разметка не должна теряться при пересборке отчёта: берём её из --gold, а если не задан — из текущего comparison.csv.
    gold_src = Path(gold_path) if gold_path else (out_dir / 'comparison.csv')
    gold = None
    if gold_src.exists():
        gmap = {int(r['id']): r for r in read_csv(gold_src) if r.get('id')}
        for r in cmp_rows:
            g = gmap.get(int(r['id']))
            if g:
                r['gold_date'], r['gold_cats'] = g.get('gold_date', '') or '', g.get('gold_cats', '') or ''
        if any(r['gold_date'] or r['gold_cats'] for r in cmp_rows):
            gold = gold_scores(cmp_rows, list(results))
    write_csv(out_dir / 'comparison.csv', cmp_rows)
    dis = sorted([r for r in cmp_rows if r['models_disagree'] or r['db_differs_from_consensus']],
                 key=lambda r: (-int(r['models_disagree']), r['id']))
    write_csv(out_dir / 'disagreements.csv', dis, list(cmp_rows[0].keys()) if cmp_rows else None)
    text = format_summary(summary, gold)
    text += f"\n\nПостов в выборке: {len(sample)} | спорных по дате: {len(dis)}\nФайлы: {out_dir}"
    (out_dir / 'report.txt').write_text(text, encoding='utf-8')
    print('\n' + text)


def load_sample_from_files(out_dir: Path) -> list[dict]:
    rows = read_csv(out_dir / 'sample.csv')
    for r in rows:
        r['id'] = int(r['id'])
        r['channel_id'] = int(r['channel_id'])
    return rows


async def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--models', default=','.join(DEFAULT_MODELS), help='через запятую')
    ap.add_argument('--limit', type=int, default=600)
    ap.add_argument('--days', type=int, help='только посты за последние N дней')
    ap.add_argument('--channel', type=int, help='только этот channel_id')
    ap.add_argument('--random', action='store_true', help='случайная выборка вместо самых свежих')
    ap.add_argument('--seed', type=int, default=1)
    ap.add_argument('--concurrency', type=int, default=3)
    ap.add_argument('--timeout', type=float, default=90)
    ap.add_argument('--max-chars', type=int, default=0, help='обрезка текста для модели (по умолчанию parsing.max_chars)')
    ap.add_argument('--out-dir')
    ap.add_argument('--resume', action='store_true', help='пропустить модели, у которых уже есть результат')
    ap.add_argument('--report-only', action='store_true')
    ap.add_argument('--gold', help='comparison.csv с заполненными gold_date / gold_cats')
    args = ap.parse_args()

    out_dir = Path(args.out_dir) if args.out_dir else Path(__file__).with_name('ai_parsing_bench') / datetime.now().strftime('%Y%m%d_%H%M')
    out_dir.mkdir(parents=True, exist_ok=True)
    models = [m.strip() for m in args.models.split(',') if m.strip()]

    from loguru import logger
    logger.remove()
    logger.add(sys.stderr, level='ERROR')

    if args.report_only:
        build_report(out_dir, models, load_sample_from_files(out_dir), args.gold, {})
        return

    from shared.config import config
    from shared.ai import client_from_config
    from collector.ai import extractor
    from collector.categorizer_v2 import category_names
    from db.connection import init_db_pool, close_db_pool

    cfg = config['parsing']
    if not cfg.get('api_key'):
        sys.exit("Нет OPENCODE_PARSING_API_KEY в окружении (.env.local)")

    def factory(model):
        return client_from_config('parsing', {**cfg, 'models': [model], 'timeout': args.timeout})

    allowed = category_names()
    pool = await init_db_pool()
    try:
        sample = await load_sample(pool, args, out_dir)
        names = await channel_names(pool)
    finally:
        await close_db_pool(pool)
    if not sample:
        sys.exit("В БД нет подходящих постов")
    write_csv(out_dir / 'sample.csv',
              [{**s, 'channel_id': s['channel_id'], 'text': s['text'][:TEXT_IN_SAMPLE]} for s in sample], SAMPLE_FIELDS)
    print(f"Выборка: {len(sample)} постов | модели: {', '.join(models)} | папка: {out_dir}", flush=True)

    for i, model in enumerate(models, 1):
        rp, jp = out_dir / f'result_{safe_name(model)}.csv', out_dir / f'result_{safe_name(model)}.json'
        if args.resume and rp.exists() and jp.exists():
            print(f"[{i}/{len(models)}] {model}: уже есть, пропускаю", flush=True)
            continue
        print(f"[{i}/{len(models)}] {model}: считаю...", flush=True)
        rows, meta = await run_model(model, sample, cfg, args, extractor, allowed, factory)
        write_csv(rp, rows, RESULT_FIELDS)
        jp.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding='utf-8')
        ok = sum(1 for r in rows if not r['error'])
        lat = [float(r['latency']) for r in rows if r['latency'] != '']
        print(f"    ответов {ok}/{len(rows)} · {meta['wall_sec']} с · p50 {pct(lat, .5):.2f} с · p95 {pct(lat, .95):.2f} с · "
              f"токены {meta['tokens_in']}/{meta['tokens_out']} · отказов {meta['failed_requests']}", flush=True)

    build_report(out_dir, models, [{**s} for s in sample], args.gold, names)


if __name__ == '__main__':
    asyncio.run(main())
