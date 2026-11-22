"""Ручной расчёт ИИ-строк (с хуками и интернет-фактами) для выбранных категорий и дней. Строки ПИШУТСЯ в БД (card_lines).

    docker compose exec deduplicator python scripts/card_lines_run.py --profiles "Концерты" --days 2
    docker compose exec deduplicator python scripts/card_lines_run.py --profiles "Концерты,Кино/Театр" --days 7 --force
    docker compose exec deduplicator python scripts/card_lines_run.py --profiles "Концерты" --days 2 --dry-run

--profiles     категории через запятую (по умолчанию все категории с ИИ-профилем)
--days         сколько дней считать, включая первый: 1 = только сегодня, 2 = сегодня и завтра (по умолчанию 2)
--start        первый день ГГГГ-ММ-ДД (по умолчанию сегодня по Белграду)
--hooks        off|on: хуки (по умолчанию как в config.yaml card_lines.hooks.method; on включает принудительно)
--enrich       off|on: интернет-факты Wikipedia и MusicBrainz (по умолчанию как в config.yaml)
--force        пересчитать и записи, у которых строка уже есть (иначе только новые и изменившиеся)
--dry-run      только показать, сколько и каких записей будет посчитано; модели не вызываются, в БД ничего не пишется
--models       модели через запятую (по умолчанию card_lines.models из config.yaml)
--max-calls    потолок запросов к моделям за запуск (по умолчанию 500)
--concurrency  параллельных запросов (по умолчанию card_lines.concurrency)
Ключ: OPENCODE_CARDLINES_API_KEY. Бот показывает строки, если в config.yaml card_lines.method: on (хуки: card_lines.hooks.method: on).
Чтобы сервис дедупликатора сам НЕ считал строки (только этот скрипт), поставь card_lines.auto: false.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('APP_ENV', 'production')

from card_lines.settings import card_line_settings  # noqa: E402

DEFAULT_MAX_CALLS = 500


def parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--profiles', default='')
    ap.add_argument('--days', type=int, default=2)
    ap.add_argument('--start', default='')
    ap.add_argument('--hooks', choices=['off', 'on'], default='')
    ap.add_argument('--enrich', choices=['off', 'on'], default='')
    ap.add_argument('--force', action='store_true')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--models', default='')
    ap.add_argument('--max-calls', type=int, default=DEFAULT_MAX_CALLS)
    ap.add_argument('--concurrency', type=int, default=0)
    args = ap.parse_args(argv)
    if args.days < 1 or args.days > 400:
        ap.error('--days: от 1 до 400')
    if args.start:
        try:
            date.fromisoformat(args.start)
        except ValueError:
            ap.error('--start: формат ГГГГ-ММ-ДД')
    return args


def build_settings(args, cfg: dict):
    """Настройки запуска: config.yaml → card_lines плюс параметры командной строки. Всегда method=on, авторежим не нужен."""
    hooks = dict(cfg.get('hooks') or {})
    enrich = dict(hooks.get('enrich') or {})
    if args.hooks:
        hooks['method'] = args.hooks
    if args.enrich:
        enrich['method'] = args.enrich
    hooks['enrich'] = enrich
    over = {**cfg, 'method': 'on', 'after_dedup': False, 'hooks': hooks, 'max_calls_per_run': args.max_calls,
            'only_profiles': [p.strip() for p in args.profiles.split(',') if p.strip()],
            'horizon_days': args.days - 1}
    if args.concurrency:
        over['concurrency'] = args.concurrency
    if args.models:
        over['models'] = [m.strip() for m in args.models.split(',') if m.strip()]
    return card_line_settings(over), over


async def main() -> None:
    args = parse_args()
    from loguru import logger
    from card_lines.select import select_candidates
    from card_lines.service import DEFAULT_CITY, run_pass
    from db.connection import close_db_pool, init_db_pool
    from shared.ai.client import client_from_config
    from shared.config import config
    from shared.tz import belgrade_today

    st, over = build_settings(args, config.get('card_lines') or {})
    start = date.fromisoformat(args.start) if args.start else belgrade_today()
    pool = await init_db_pool()
    try:
        hint = (f"{start} + {args.days} дн., категории: {list(st.only_profiles) or 'все'}, хуки: {st.hooks.method}, "
                f"интернет: {st.hooks.enrich.method}, force={args.force}")
        if args.dry_run:
            sel = await select_candidates(pool, st, today=start, horizon_days=args.days - 1, city=DEFAULT_CITY,
                                          include_done=args.force)
            print(f"Пробный просмотр ({hint})")
            print(f"Всего записей в периоде: {sel.total}, актуальных строк уже есть: {sel.up_to_date}, "
                  f"вне выбранных категорий или без ИИ-профиля: {sel.no_profile}")
            print(f"К расчёту: {len(sel.todo)} {dict(sel.by_profile)}")
            for c in sel.todo[:30]:
                print(f"  {c.kind}{c.id} | {c.profile.name} | {(c.title or c.body[:60]).strip()[:70]}")
            return
        client = client_from_config('card-lines', over)
        if not client.configured:
            print("Нет OPENCODE_CARDLINES_API_KEY или card_lines.models: запуск невозможен", file=sys.stderr)
            sys.exit(2)
        print(f"Запуск ({hint}); модели: {client.models}")
        res = await run_pass(pool, client, st, city=DEFAULT_CITY, horizon_days=args.days - 1, today=start, force=args.force)
        print(f"\nСтатус: {res.status}" + (f" ({res.error})" if res.error else ''))
        print(f"К расчёту было: {res.candidates} {dict(res.by_profile)}")
        print(f"Готово: {res.generated} (скопировано по хэшу: {res.copied}), не прошли проверку названия: {res.failed}, "
              f"осталось: {res.remaining}, запросов к моделям: {res.calls}")
        print(f"Хуки: {dict(res.hooks)}")
        if res.status in ('partial', 'incomplete'):
            print("Часть записей не посчитана (лимит запросов или модели недоступны): запусти скрипт ещё раз.")
    finally:
        await close_db_pool(pool)


if __name__ == '__main__':
    asyncio.run(main())
