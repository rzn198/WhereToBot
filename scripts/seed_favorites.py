"""Разовая засыпка «Запланированного» тестовыми записями: до N событий каждой категории на каждый день вперёд.

Берёт ровно те записи, которые пользователь видит в ленте категории на этот день (посты и события, без скрытых админом и
без скрытых самим пользователем), первые N по порядку ленты. Запись из нескольких категорий считается один раз.
Всё, что скрипт добавил, пишется в JSON-файл — по нему добавленное снимается командой --undo (чужое и прежнее избранное не трогается).

  python -m scripts.seed_favorites --user 123456789                     # только отчёт (ничего не меняет)
  python -m scripts.seed_favorites --user 123456789 --apply             # добавить в избранное
  python -m scripts.seed_favorites --user 123456789 --apply --per-category 5 --days 14 --categories concerts,sport
  python -m scripts.seed_favorites --undo /app/logs/seed_favorites_123456789_20261127_140000.json
Без --user берётся telegram.admin_user_id из config.yaml.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('APP_ENV', 'production')

from bot.handlers.events import load_event_ids  # noqa: E402
from bot.utils.categories import CATEGORIES, get_category  # noqa: E402
from bot.utils.refs import is_event_ref, ref_id  # noqa: E402
from db.connection import close_db_pool, init_db_pool  # noqa: E402
from db.tables import Tables  # noqa: E402
from shared.tz import belgrade_today  # noqa: E402


@dataclass
class Report:
    per_category: dict[str, int] = field(default_factory=dict)   # код категории → сколько записей выбрано (с повторами между категориями)
    messages: set[int] = field(default_factory=set)              # выбранные посты (уникальные)
    events: set[int] = field(default_factory=set)                # выбранные события (уникальные)
    new_messages: list[int] = field(default_factory=list)        # реально добавлено (раньше не было в избранном)
    new_events: list[int] = field(default_factory=list)
    applied: bool = False

    @property
    def unique(self) -> int:
        return len(self.messages) + len(self.events)


async def select_refs(pool, user_id: int, codes: list[str], per_category: int, days: int) -> Report:
    rep = Report()
    start = belgrade_today()
    for code in codes:
        total = 0
        for i in range(days):
            day = start + timedelta(days=i)
            refs = (await load_event_ids(pool, user_id, f'cat:{code}:date:{day.isoformat()}'))[:per_category]
            for ref in refs:
                (rep.events if is_event_ref(ref) else rep.messages).add(ref_id(ref))
            total += len(refs)
        rep.per_category[code] = total
    return rep


async def _insert(pool, table: str, column: str, user_id: int, ids: set[int]) -> list[int]:
    if not ids:
        return []
    rows = await pool.fetch(
        f"INSERT INTO {table} (user_id, {column}) SELECT $1, x FROM unnest($2::bigint[]) x "
        f"ON CONFLICT DO NOTHING RETURNING {column}", user_id, sorted(ids))
    return sorted(r[column] for r in rows)


async def seed(pool, user_id: int, *, codes: list[str], per_category: int, days: int, apply: bool) -> Report:
    rep = await select_refs(pool, user_id, codes, per_category, days)
    if apply:
        rep.new_messages = await _insert(pool, Tables.FAVORITES, 'msg_id', user_id, rep.messages)
        rep.new_events = await _insert(pool, Tables.EVENT_FAVORITES, 'event_id', user_id, rep.events)
        rep.applied = True
    return rep


async def undo(pool, path: str) -> tuple[int, int]:
    """Снять только то, что добавил скрипт (по файлу). Возвращает (постов, событий) снято."""
    with open(path, encoding='utf-8') as f:
        data = json.load(f)
    uid = int(data['user_id'])
    m = await pool.execute(f"DELETE FROM {Tables.FAVORITES} WHERE user_id = $1 AND msg_id = ANY($2::bigint[])",
                           uid, data['messages'])
    e = await pool.execute(f"DELETE FROM {Tables.EVENT_FAVORITES} WHERE user_id = $1 AND event_id = ANY($2::bigint[])",
                           uid, data['events'])
    return int(m.split()[-1]), int(e.split()[-1])


def save_report(rep: Report, user_id: int, directory: str) -> str:
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"seed_favorites_{user_id}_{datetime.now():%Y%m%d_%H%M%S}.json")
    with open(path, 'w', encoding='utf-8') as f:
        json.dump({'user_id': user_id, 'messages': rep.new_messages, 'events': rep.new_events}, f)
    return path


def _codes(arg: str | None) -> list[str]:
    if not arg:
        return [c.code for c in CATEGORIES]
    codes = [c.strip() for c in arg.split(',') if c.strip()]
    bad = [c for c in codes if get_category(c) is None]
    if bad:
        raise SystemExit(f"Неизвестные категории: {', '.join(bad)}. Доступные: {', '.join(c.code for c in CATEGORIES)}")
    return codes


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--user', type=int, help='Telegram user_id (по умолчанию telegram.admin_user_id из config.yaml)')
    ap.add_argument('--per-category', type=int, default=10)
    ap.add_argument('--days', type=int, default=30)
    ap.add_argument('--categories', help='коды через запятую (по умолчанию все)')
    ap.add_argument('--apply', action='store_true', help='записать в БД (без флага — только отчёт)')
    ap.add_argument('--undo', metavar='FILE', help='снять добавленное по файлу отчёта')
    args = ap.parse_args()

    from shared.logging import LOGS_DIR, setup_logging
    setup_logging('seed_favorites')
    pool = await init_db_pool()
    try:
        if args.undo:
            m, e = await undo(pool, args.undo)
            print(f"Снято из избранного: постов {m}, событий {e}")
            return 0
        if args.per_category < 1 or args.days < 1:
            raise SystemExit('--per-category и --days должны быть не меньше 1')
        user_id = args.user
        if user_id is None:
            from shared.config import config
            user_id = int(config['telegram']['admin_user_id'])
        codes = _codes(args.categories)
        rep = await seed(pool, user_id, codes=codes, per_category=args.per_category, days=args.days, apply=args.apply)
        print(f"Пользователь {user_id}: до {args.per_category} на категорию в день, {args.days} дн., категорий {len(codes)}")
        for code, n in rep.per_category.items():
            print(f"  {get_category(code).emoji} {get_category(code).label:<28} {n:>4}")
        print(f"Уникальных записей: {rep.unique} (постов {len(rep.messages)}, событий {len(rep.events)})")
        if not args.apply:
            print("\nРежим просмотра: ничего не изменено. Для записи добавьте --apply")
            return 0
        path = save_report(rep, user_id, str(LOGS_DIR))
        print(f"Добавлено новых: постов {len(rep.new_messages)}, событий {len(rep.new_events)} "
              f"(остальное уже было в избранном)\nФайл для отмены: {path}")
        return 0
    finally:
        await close_db_pool()


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
