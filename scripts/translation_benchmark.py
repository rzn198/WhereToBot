"""Бенчмарк моделей для перевода описаний событий. В БД ничего не пишет.

    python scripts/translation_benchmark.py --limit 40                      # выборка из БД, модели из translation.models
    python scripts/translation_benchmark.py --models gpt-6-luna,glm-5.3-flash,deepseek-v4.1-flash --limit 60
    python scripts/translation_benchmark.py --from-json sample.json         # свои описания без БД
    python scripts/translation_benchmark.py --resume scripts/tr_bench/20261116_190000   # продолжить после обрыва/лимита
    python scripts/translation_benchmark.py --rate scripts/tr_bench/<папка>/comparison.csv   # итог по вашим оценкам

Что делает: берёт описания НЕ на русском из источников translation.sources (по кругу по источникам, воспроизводимо
по --seed), каждый отправляет КАЖДОЙ модели тем же промптом и теми же ограничениями, что и боевой переводчик
(translator.text: SYSTEM_PROMPT, cut_input, validate). Каждая модель вызывается отдельно, без переключения на запасную.

Автоматические метрики (по каждой модели): доля успешных ответов, доля «годных» (русский, не копия оригинала,
числа/даты/время сохранены, нет вводных фраз и разметки), задержка p50/p95, токены, длина относительно оригинала.
Качество самого перевода оцениваете вы: comparison.csv (оригинал + переводы всех моделей рядом, столбцы оценки 1–5),
затем --rate покажет средние оценки.

Результаты: scripts/tr_bench/<дата-время>/ : sample.json, results.jsonl (по строке на пару модель×описание, пишется
сразу — после обрыва можно --resume), summary.csv, comparison.csv, report.txt.
Ключ: OPENCODE_TRANSLATE_API_KEY (как у переводчика); --use-parsing-key подставит ключ парсинга. БД: как в остальных скриптах."""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import random
import re
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8')
    except Exception:
        pass
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
os.environ.setdefault('APP_ENV', 'local')

from translator.text import SYSTEM_PROMPT, BadTranslation, cut_input, is_russian, validate  # noqa: E402

DEAD_AFTER = 4            # столько подряд отказов модели (сеть/ключ/лимит) — остальное по ней пропускаем
_NUM = re.compile(r"\d+")
_URL = re.compile(r"https?://\S+")
_LAT = re.compile(r"[A-Za-zČčĆćŽžŠšĐđ]")
_CYR = re.compile(r"[а-яёА-ЯЁ]")
_PREAMBLE = re.compile(r"^\s*(вот\s+перевод|перевод\s*[:\-—]|translation\s*[:\-—]|here\s+is|here's|конечно|sure[,!:]|"
                       r"ниже\s+(представлен\s+)?перевод)", re.I)
_META = re.compile(r"(\n\s*(примечание|note|комментарий)\s*[:\-—])|```", re.I)


# ----------------------------------------------------------------------------------------------- метрики
def numbers(text: str) -> set[str]:
    return set(_NUM.findall(text or ''))


def analyze(original: str, output: str) -> dict:
    """Автоматические признаки одного перевода. Чистая функция."""
    original, output = original.strip(), output.strip()
    nums_in = numbers(original)
    nums_kept = (len(nums_in & numbers(output)) / len(nums_in)) if nums_in else 1.0
    urls_in = set(_URL.findall(original))
    urls_kept = all(u.rstrip('.,;)') in output for u in urls_in) if urls_in else True
    lat, cyr = len(_LAT.findall(output)), len(_CYR.findall(output))
    paras_in, paras_out = len([p for p in original.split('\n\n') if p.strip()]), len([p for p in output.split('\n\n') if p.strip()])
    unchanged = re.sub(r'\s+', ' ', original).lower() == re.sub(r'\s+', ' ', output).lower()
    junk = bool(_PREAMBLE.search(output) or _META.search(output)
                or (len(output) > 1 and output[0] in '"«“' and output[-1] in '"»”' and output.count('"') + output.count('«') <= 2))
    a = dict(
        russian=is_russian(output),
        unchanged=unchanged,
        numbers_kept=round(nums_kept, 3),
        urls_kept=urls_kept,
        junk=junk,
        paragraphs_lost=paras_in >= 2 and paras_out == 1,
        latin_share=round(lat / (lat + cyr), 3) if lat + cyr else 0.0,
        len_ratio=round(len(output) / max(len(original), 1), 2),
        ends_cleanly=bool(output) and (output[-1] in '.!?…»"”)' or output[-1].isdigit() or not original[-1:] in '.!?…'),
    )
    a['good'] = bool(a['russian'] and not unchanged and nums_kept == 1.0 and urls_kept and not junk and not a['paragraphs_lost'])
    return a


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    return s[min(len(s) - 1, int(round(p * (len(s) - 1))))]


def summarize(results: list[dict], models: list[str]) -> list[dict]:
    out = []
    for m in models:
        rows = [r for r in results if r['model'] == m]
        done = [r for r in rows if r['status'] != 'skipped']
        ok = [r for r in done if r['status'] == 'ok']
        good = [r for r in ok if r.get('good')]
        times = [r['seconds'] for r in ok]
        n = len(done)
        out.append(dict(
            model=m, items=n, ok=len(ok), ok_pct=round(100 * len(ok) / n, 1) if n else 0,
            good=len(good), good_pct=round(100 * len(good) / n, 1) if n else 0,
            not_russian=sum(1 for r in ok if not r['russian']), unchanged=sum(1 for r in ok if r['unchanged']),
            numbers_lost=sum(1 for r in ok if r['numbers_kept'] < 1.0), junk=sum(1 for r in ok if r['junk']),
            bad_output=sum(1 for r in done if r['status'] == 'bad_output'), failed=sum(1 for r in done if r['status'] == 'failed'),
            skipped=len(rows) - n,
            p50_s=round(percentile(times, 0.5), 1), p95_s=round(percentile(times, 0.95), 1),
            avg_tokens_in=round(statistics.fmean([r['tokens_in'] for r in ok]), 0) if ok else 0,
            avg_tokens_out=round(statistics.fmean([r['tokens_out'] for r in ok]), 0) if ok else 0,
            avg_len_ratio=round(statistics.fmean([r['len_ratio'] for r in ok]), 2) if ok else 0,
        ))
    return out


# ----------------------------------------------------------------------------------------------- выборка
def pick_sample(rows: list[dict], limit: int, seed: int) -> list[dict]:
    """По кругу по источникам (чтобы один источник не занял всю выборку), внутри источника случайно по seed."""
    rng = random.Random(seed)
    by_src: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_src[r['source']].append(r)
    for lst in by_src.values():
        rng.shuffle(lst)
    out, srcs = [], sorted(by_src)
    while len(out) < limit and any(by_src.values()):
        for s in srcs:
            if by_src[s] and len(out) < limit:
                out.append(by_src[s].pop())
    return out


async def load_from_db(sources: list[str], min_chars: int) -> list[dict]:
    from db.connection import close_db_pool, init_db_pool
    from db.tables import Tables
    pool = await init_db_pool()
    try:
        rows = await pool.fetch(
            f"SELECT id, source, source_url, title, description FROM {Tables.EVENTS} "
            f"WHERE lower(source) = ANY($1::text[]) AND description IS NOT NULL AND length(btrim(description)) >= $2 "
            f"AND NOT hidden_by_admin", [s.lower() for s in sources], min_chars)
    finally:
        await close_db_pool(pool)
    return [dict(r) for r in rows if not is_russian(r['description'])]


# ----------------------------------------------------------------------------------------------- прогон
async def run_model(model: str, items: list[dict], args, tcfg: dict, done: set, sink) -> None:
    from shared.ai import AIClient, AIUnavailable
    todo = [it for it in items if (model, str(it['id'])) not in done]
    if not todo:
        return
    sem = asyncio.Semaphore(max(1, args.concurrency))
    state = {'fails': 0}

    async def one(it: dict) -> None:
        sent = cut_input(it['description'], int(tcfg.get('input_chars') or 1500))
        row = dict(model=model, id=str(it['id']), source=it['source'], status='ok', seconds=0.0, tokens_in=0, tokens_out=0,
                   input=sent, output='', error='')
        async with sem:
            if state['fails'] >= DEAD_AFTER:
                row.update(status='skipped', error=f'модель отказала {DEAD_AFTER} раза подряд')
                sink(row)
                return
            client = AIClient('bench', tcfg['api_key'], [model], base_url=tcfg.get('base_url') or None or _default_base(),
                              timeout=args.timeout, temperature=tcfg.get('temperature'))
            t = time.monotonic()
            try:
                out = await client.complete(SYSTEM_PROMPT, sent)
                row['seconds'] = round(time.monotonic() - t, 2)
                row['tokens_in'], row['tokens_out'] = client.stats.prompt_tokens, client.stats.completion_tokens
                validate(out, sent)
                row['output'] = out
                row.update(analyze(sent, out))
                state['fails'] = 0
            except BadTranslation as e:
                row.update(status='bad_output', error=str(e), output=locals().get('out', ''), seconds=round(time.monotonic() - t, 2))
                state['fails'] = 0
            except AIUnavailable as e:
                row.update(status='failed', error=str(e), seconds=round(time.monotonic() - t, 2))
                state['fails'] += 1
            except Exception as e:
                row.update(status='failed', error=f"{type(e).__name__}: {e}", seconds=round(time.monotonic() - t, 2))
                state['fails'] += 1
            finally:
                await client.aclose()
        sink(row)

    await asyncio.gather(*(one(it) for it in todo))


def _default_base() -> str:
    from shared.ai.client import DEFAULT_BASE_URL
    return DEFAULT_BASE_URL


# ----------------------------------------------------------------------------------------------- отчёты
def write_outputs(out: Path, items: list[dict], models: list[str], results: list[dict]) -> str:
    summary = summarize(results, models)
    head = list(summary[0].keys()) if summary else ['model']
    with open(out / 'summary.csv', 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=head)
        w.writeheader()
        w.writerows(summary)

    by = {(r['model'], r['id']): r for r in results}
    cols = ['id', 'source', 'title', 'original'] + [c for m in models for c in (f'{m}', f'оценка_{m}')]
    with open(out / 'comparison.csv', 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f)
        w.writerow(cols)
        for it in items:
            row = [it['id'], it['source'], it.get('title', ''), cut_input(it['description'], 1500)]
            for m in models:
                r = by.get((m, str(it['id'])))
                text = '' if r is None else (r['output'] if r['status'] == 'ok' else f"[{r['status']}] {r['error']}")
                if r and r['status'] == 'ok' and not r.get('good'):
                    flags = [k for k, bad in (('не русский', not r['russian']), ('копия', r['unchanged']),
                                              ('потеряны числа', r['numbers_kept'] < 1.0), ('вводная фраза/разметка', r['junk']),
                                              ('потеряны абзацы', r['paragraphs_lost'])) if bad]
                    text = f"[⚠ {', '.join(flags)}] {text}"
                row += [text, '']
            w.writerow(row)

    lines = [f"Описаний в выборке: {len(items)} · источники: {dict(sorted(defaultdict(int, {s: sum(1 for i in items if i['source'] == s) for s in {i['source'] for i in items}}).items()))}", '']
    lines.append(f"{'модель':28} {'ок%':>5} {'годных%':>8} {'не-рус':>6} {'копия':>5} {'числа':>5} {'мусор':>5} {'сбой':>4} {'p50с':>5} {'p95с':>5} {'ток.вых':>7}")
    for s in sorted(summary, key=lambda x: (-x['good_pct'], x['p50_s'])):
        lines.append(f"{s['model']:28} {s['ok_pct']:5} {s['good_pct']:8} {s['not_russian']:6} {s['unchanged']:5} {s['numbers_lost']:5} "
                     f"{s['junk']:5} {s['failed'] + s['bad_output']:4} {s['p50_s']:5} {s['p95_s']:5} {s['avg_tokens_out']:7.0f}")
    lines += ['', '«годных» = ответ на русском, не копия оригинала, все числа/даты на месте, нет вводных фраз и разметки.',
              'Качество формулировок оцените в comparison.csv (столбцы оценка_*, 1–5), затем: --rate <comparison.csv>']
    text = '\n'.join(lines)
    (out / 'report.txt').write_text(text, encoding='utf-8')
    return text


def rate(path: Path) -> None:
    with open(path, encoding='utf-8-sig', newline='') as f:
        rows = list(csv.DictReader(f))
    cols = [c for c in rows[0] if c.startswith('оценка_')] if rows else []
    print(f"{'модель':28} {'оценок':>6} {'средняя':>8}   по источникам")
    for c in sorted(cols, key=lambda c: -(statistics.fmean([float(r[c]) for r in rows if str(r[c]).strip().replace(',', '.').replace('.', '', 1).isdigit()] or [0]))):
        vals = [(r['source'], float(r[c].replace(',', '.'))) for r in rows if str(r[c]).strip().replace(',', '.').replace('.', '', 1).isdigit()]
        per = defaultdict(list)
        for s, v in vals:
            per[s].append(v)
        print(f"{c[len('оценка_'):]:28} {len(vals):6} {statistics.fmean([v for _, v in vals]) if vals else 0:8.2f}   "
              + ', '.join(f"{s}: {statistics.fmean(v):.1f}" for s, v in sorted(per.items())))


# ----------------------------------------------------------------------------------------------- main
async def main_async(args) -> int:
    if args.rate:
        rate(Path(args.rate))
        return 0
    from shared.config import config
    tcfg = dict(config['translation'])
    tcfg['api_key'] = tcfg.get('api_key') or (config['parsing'].get('api_key') if args.use_parsing_key else '') or ''
    if args.base_url:
        tcfg['base_url'] = args.base_url
    models = [m.strip() for m in args.models.split(',')] if args.models else list(tcfg.get('models') or [])
    if not tcfg['api_key'] or not models:
        sys.exit("Нужны OPENCODE_TRANSLATE_API_KEY (или --use-parsing-key) и модели (--models или translation.models).")
    args.timeout = args.timeout or float(tcfg.get('timeout') or 60)

    if args.resume:
        out = Path(args.resume)
        items = json.loads((out / 'sample.json').read_text(encoding='utf-8'))
    else:
        out = Path(args.out_dir) / datetime.now().strftime('%Y%m%d_%H%M%S')
        out.mkdir(parents=True, exist_ok=True)
        if args.from_json:
            rows = json.loads(Path(args.from_json).read_text(encoding='utf-8'))
            rows = [r for r in rows if not is_russian(r['description'])]
        else:
            sources = [s.strip() for s in args.sources.split(',')] if args.sources else list(config['translation'].get('sources') or [])
            rows = await load_from_db(sources, args.min_chars)
        items = pick_sample(rows, args.limit, args.seed)
        (out / 'sample.json').write_text(json.dumps(items, ensure_ascii=False, indent=1, default=str), encoding='utf-8')
    if not items:
        sys.exit("Выборка пуста: нет подходящих описаний не на русском.")
    print(f"Описаний: {len(items)}, моделей: {len(models)} ({', '.join(models)}). Папка: {out}", flush=True)

    jl = out / 'results.jsonl'
    results = [json.loads(line) for line in jl.read_text(encoding='utf-8').splitlines() if line.strip()] if jl.exists() else []
    done = {(r['model'], r['id']) for r in results if r['status'] in ('ok', 'bad_output')}   # failed/skipped пробуем снова
    results = [r for r in results if (r['model'], r['id']) in done]
    jl.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in results), encoding='utf-8')

    def sink(row: dict) -> None:
        results.append(row)
        with open(jl, 'a', encoding='utf-8') as f:
            f.write(json.dumps(row, ensure_ascii=False) + '\n')

    for m in models:
        before = len(results)
        await run_model(m, items, args, tcfg, done, sink)
        new = [r for r in results[before:] if r['model'] == m]
        print(f"  {m}: {sum(r['status'] == 'ok' for r in new)}/{len(new)} ок", flush=True)

    print('\n' + write_outputs(out, items, models, results))
    print(f"\nФайлы: {out}")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--models', help='модели через запятую (по умолчанию translation.models)')
    ap.add_argument('--limit', type=int, default=40, help='сколько описаний взять (по умолчанию 40)')
    ap.add_argument('--sources', help='источники через запятую (по умолчанию translation.sources)')
    ap.add_argument('--min-chars', type=int, default=80, help='минимальная длина описания')
    ap.add_argument('--seed', type=int, default=1)
    ap.add_argument('--from-json', help='свои описания: список {id, source, title, description, source_url}')
    ap.add_argument('--resume', help='папка прежнего запуска')
    ap.add_argument('--rate', help='comparison.csv с вашими оценками: показать средние')
    ap.add_argument('--concurrency', type=int, default=3)
    ap.add_argument('--timeout', type=float, default=0, help='таймаут запроса, сек (0 — из конфига)')
    ap.add_argument('--use-parsing-key', action='store_true')
    ap.add_argument('--base-url', help='другой адрес AI-шлюза')
    ap.add_argument('--out-dir', default=str(HERE / 'tr_bench'))
    sys.exit(asyncio.run(main_async(ap.parse_args())))


if __name__ == '__main__':
    main()
