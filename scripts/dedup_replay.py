"""Сухой прогон AI-дедупликации по данным в БД: ничего не меняет и не пишет в БД.

Показывает, какие группы дублей нашёл бы AI, кого скрыл бы и кого оставил, и сверяет с тем, что раньше
скрыл старый дедупликатор (SequenceMatcher). Нужен, чтобы проверить качество и выбрать модель ДО включения.

    python scripts/dedup_replay.py                         # будущие даты, модели из deduplication.ai.models
    python scripts/dedup_replay.py --models deepseek-v4.1-flash,gpt-6-luna --max-days 30
    python scripts/dedup_replay.py --from 2026-09-01       # включая прошлые даты (чтобы сверить со старым хуком)
    python scripts/dedup_replay.py --use-parsing-key       # взять ключ парсинга, если OPENCODE_DEDUP_API_KEY ещё нет

Скрытые записи в этом режиме считаются видимыми (иначе автоскрытия старого хука не попали бы в сравнение);
--respect-hidden возвращает обычное поведение. Результат (папка scripts/dedup_runs):
    dedup_clusters_<модель>.csv   группы: победитель, проигравшие, источник, текст, причина, метка старого хука
Приоритеты берутся из deduplication.ai.priority; каналы по @имени — из таблицы channels (её заполняет коллектор).
"""
import argparse
import asyncio
import csv
import os
import re
import sys
from collections import Counter
from datetime import date
from pathlib import Path

if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
os.environ.setdefault('APP_ENV', 'local')

from db.tables import Tables  # noqa: E402
from deduplicator import runner  # noqa: E402
from deduplicator.engine import DayPlan, order_group  # noqa: E402
from deduplicator.priority import PriorityResolver  # noqa: E402
from shared.ai import client_from_config  # noqa: E402

FIELDS = ['day', 'group', 'role', 'kind', 'id', 'source', 'position', 'legacy', 'text', 'reason', 'model']


def cluster_rows(plans: list[DayPlan], resolver: PriorityResolver, legacy: dict, model: str) -> list[dict]:
    """Строки CSV: по одной на запись группы. role: winner | loser | kept (тот же источник) | kept (дайджест)."""
    rows = []
    for plan in plans:
        losers = {nd.loser.key for nd in plan.wanted}
        for gi, g in enumerate(plan.groups, 1):
            members = sorted(g.members, key=resolver.rank)
            winner, rest = order_group(members, plan.lists, plan.weak, resolver)
            if winner is None or not any(m.source != winner.source for m in rest):
                continue
            skipped = [x for x in members if x.key != winner.key and x.key not in {y.key for y in rest}]
            extra = [nd.loser for nd in plan.wanted if nd.winner.key == winner.key
                     and nd.loser.key not in {y.key for y in members}]            # точные копии, добавленные движком
            for m in [winner] + rest + skipped + extra:                           # победитель всегда первым
                if m.key == winner.key:
                    role = 'winner'
                elif m.key in losers:
                    role = 'loser'
                else:
                    role = ('kept (перечень)' if m.key in plan.lists else 'kept (дайджест)' if m.is_digest
                            else 'kept (тот же источник)')
                rows.append(dict(
                    day=plan.day.isoformat(), group=f"{plan.day.isoformat()}#{gi}", role=role,
                    kind='message' if m.kind == 'm' else 'event', id=m.id, source=resolver.label(m),
                    position=m.position, legacy=legacy.get(m.key, ''),
                    text=(m.text or '').strip(), reason=g.reason if m.key == winner.key else '',
                    model=model))
    return rows


def loser_pairs(plans: list[DayPlan]) -> set[tuple]:
    return {(nd.loser.key, nd.winner.key) for p in plans for nd in p.wanted}


def legacy_hidden(plans: list[DayPlan], legacy: dict) -> tuple[int, int]:
    """(скрытых старым хуком записей, из них AI тоже признал проигравшими)."""
    ai_losers = {nd.loser.key for p in plans for nd in p.wanted}
    old = {k for k, v in legacy.items() if v == 'старый хук'}
    return len(old), len(old & ai_losers)


async def load_legacy_marks(pool, start: date) -> dict:
    rows = await pool.fetch(
        f"SELECT surrogate_id, hidden_by_admin, duplicate_of FROM {Tables.MESSAGES} "
        f"WHERE date >= $1 AND hidden_by_admin = TRUE", start)
    out = {('m', r['surrogate_id']): 'старый хук' if r['duplicate_of'] else 'скрыт вручную' for r in rows}
    for r in await pool.fetch(f"SELECT id FROM {Tables.EVENTS} WHERE starts_at >= $1 AND hidden_by_admin = TRUE", start):
        out[('e', r['id'])] = 'скрыт вручную'
    return out


def save_csv(path: Path, rows: list[dict]) -> None:
    with open(path, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)


def print_model_summary(model, res, rows, plans, legacy):
    st = res.stats
    losers = sum(len(p.wanted) for p in plans)
    print(f"   дат: {st['dates_processed']} из {st['dates_total']} · запросов {st['requests']} · "
          f"tokens in/out {st['tokens_in']}/{st['tokens_out']} · групп {st['groups_found']} · проигравших {losers} · "
          f"плохих ответов {st['bad_days']}")
    by_pair = Counter()
    for p in plans:
        for nd in p.wanted:
            by_pair[(res.resolver.label(nd.winner), res.resolver.label(nd.loser))] += 1
    for (w, l), n in by_pair.most_common(12):
        print(f"      {w} побеждает {l}: {n}")
    old, both = legacy_hidden(plans, legacy)
    if old:
        print(f"   старый хук скрыл {old} записей, из них AI тоже признал дублями: {both}")


async def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--models', help='через запятую; по умолчанию deduplication.ai.models')
    ap.add_argument('--from', dest='start', help='начальная дата ГГГГ-ММ-ДД (по умолчанию сегодня)')
    ap.add_argument('--max-days', type=int, default=0, help='обработать не больше N дат (0 — все)')
    ap.add_argument('--concurrency', type=int, default=3)
    ap.add_argument('--timeout', type=float, default=120)
    ap.add_argument('--max-records', type=int, default=0,
                    help='записей в одном запросе (по умолчанию deduplication.ai.max_records_per_request, 80); '
                         'для сравнения нарезок. Результаты разных запусков кладите в разные --out-dir')
    ap.add_argument('--respect-hidden', action='store_true', help='не считать скрытые записи видимыми')
    ap.add_argument('--use-parsing-key', action='store_true', help='взять OPENCODE_PARSING_API_KEY, если нет ключа дедупа')
    ap.add_argument('--out-dir', default=str(HERE / 'dedup_runs'))
    args = ap.parse_args()

    from shared.config import config
    from db.connection import init_db_pool, close_db_pool
    from loguru import logger
    logger.remove()
    logger.add(sys.stderr, level='WARNING')

    cfg = config['deduplication']
    ai = dict(cfg['ai'])
    key = ai.get('api_key') or (config['parsing'].get('api_key') if args.use_parsing_key else '')
    if not key:
        sys.exit('Нет OPENCODE_DEDUP_API_KEY (или добавь --use-parsing-key, чтобы взять ключ парсинга)')
    models = [m.strip() for m in args.models.split(',')] if args.models else ai.get('models')
    if not models:
        sys.exit('Нет моделей: укажи --models или deduplication.ai.models в config.yaml')
    start = date.fromisoformat(args.start) if args.start else None
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    pool = await init_db_pool()
    try:
        legacy = await load_legacy_marks(pool, start or date.today())
        per_model = {}
        for i, model in enumerate(models, 1):
            print(f"[{i}/{len(models)}] {model}: сравниваю...", flush=True)
            client = client_from_config('dedup', {**ai, 'api_key': key, 'models': [model], 'timeout': args.timeout})
            run_cfg = {'ai': {**ai, 'concurrency': args.concurrency,
                              **({'max_records_per_request': args.max_records} if args.max_records else {})}}
            try:
                res = await runner.run_dedup(pool, client, run_cfg, method='shadow', dry_run=True, force=True,
                                             start_date=start, ignore_hidden=not args.respect_hidden,
                                             max_days=args.max_days or None, ignore_decisions=True)
            finally:
                await client.aclose()
            if res.status != 'ok':
                print(f"   !!! {model}: {res.status} — {res.error}")
                continue
            rows = cluster_rows(res.plans, res.resolver, legacy, model)
            path = out_dir / f"dedup_clusters_{re.sub(r'[^A-Za-z0-9._-]', '_', model)}.csv"
            save_csv(path, rows)
            per_model[model] = loser_pairs(res.plans)
            print_model_summary(model, res, rows, res.plans, legacy)
            print(f"   → {path}")
        if len(per_model) > 1:
            print("\n=== Согласие моделей по решениям «кто кого скрыл» ===")
            names = list(per_model)
            for a in range(len(names)):
                for b in range(a + 1, len(names)):
                    x, y = per_model[names[a]], per_model[names[b]]
                    union = len(x | y)
                    print(f"  {names[a]} ↔ {names[b]}: общих {len(x & y)} из {union} "
                          f"({100 * len(x & y) / union:.0f}%)" if union else f"  {names[a]} ↔ {names[b]}: дублей нет")
        print("\nПришли CSV из папки dedup_runs и вывод консоли.")
    finally:
        await close_db_pool(pool)


if __name__ == '__main__':
    asyncio.run(main())
