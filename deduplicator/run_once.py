"""Ручной запуск дедупликатора и откат (команда на сервере).

  docker compose exec deduplicator python -m deduplicator.run_once [--force] [--day ГГГГ-ММ-ДД ...] [--max-days N]
                                                                  [--no-alert] [--ignore-collectors]
  docker compose exec deduplicator python -m deduplicator.run_once --release [--yes] [--include-admin]

Метод берётся ТОЛЬКО из конфига (deduplication.method: shadow | ai): флага --method нет, чтобы случайно не запустить ai.
  --force       пересчитать все даты, игнорируя сохранённые отпечатки (после смены priority, моделей, промпта)
  --day / --max-days   ограничить прогон датами
  --release     вернуть всё, что скрыли решения AI (без --yes только показывает, что будет возвращено).
                Работает лишь при method != ai: сначала смените метод в конфиге и перезапустите deduplicator,
                иначе следующий прогон скроет всё снова.
Прогон и сервисный tick не пересекаются (advisory-блокировка). Перевод победителей ручной запуск не запускает —
он выполнится после ближайшего обычного прогона сервиса.
Код возврата: 0 — готово, 1 — прогон не выполнен/ошибка, 2 — отказ (неверный режим), 3 — занято.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import date

if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

os.environ.setdefault('APP_ENV', 'production')

from loguru import logger

from db import dedup_admin
from deduplicator import notify
from deduplicator.lock import try_lock
from deduplicator.runner import run_dedup
from deduplicator.schedule import load_snapshot, service_settings


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--force', action='store_true', help='пересчитать все даты, игнорируя отпечатки')
    ap.add_argument('--day', action='append', default=[], help='ГГГГ-ММ-ДД; можно несколько раз')
    ap.add_argument('--max-days', type=int, default=0, help='не больше N дат (0 — все)')
    ap.add_argument('--no-alert', action='store_true', help='не слать отчёт админу')
    ap.add_argument('--ignore-collectors', action='store_true', help='запускать, даже если работают коллекторы')
    ap.add_argument('--release', action='store_true', help='вернуть всё, что скрыл AI (откат)')
    ap.add_argument('--yes', action='store_true', help='с --release: выполнить (без него только показать)')
    ap.add_argument('--include-admin', action='store_true', help='с --release: вернуть и решения админа')
    return ap


async def run_once(pool, cfg: dict, args, *, client=None, send=None, out=print) -> int:
    """cfg — секция deduplication. client/send/out подменяются в тестах."""
    method = cfg.get('method', 'legacy')

    async with try_lock(pool) as got:
        if not got:
            out('Дедупликатор уже работает (идёт другой прогон). Повторите позже.')
            return 3

        if args.release:
            if method == 'ai':
                out("Отказ: deduplication.method = ai — следующий прогон скроет всё снова.\n"
                    "Сначала поменяйте метод в конфиге на shadow (или legacy), перезапустите deduplicator и повторите.")
                return 2
            res = await dedup_admin.release_ai_hidden(pool, include_admin=args.include_admin, apply=args.yes)
            days = res['days']
            out(f"{'Возвращено' if res['applied'] else 'Будет возвращено (добавьте --yes)'}: постов {res['posts']}, "
                f"событий {res['events']}, решений {res['decisions']}"
                + (f", даты {days[0]} … {days[-1]}" if days else ''))
            if res['applied'] and not args.no_alert and cfg.get('service', {}).get('alerts', True):
                await (send or notify.send_admin)(
                    f"↩️ Дедупликатор: возвращено скрытых AI — постов {res['posts']}, событий {res['events']}")
            return 0

        if method not in ('shadow', 'ai'):
            out(f"Отказ: deduplication.method = {method}; ручной запуск работает для shadow и ai.")
            return 2
        if not args.ignore_collectors:
            snap = await load_snapshot(pool, service_settings(cfg))
            if snap.active_collectors:
                out(f"Работают коллекторы ({snap.active_collectors}): данные в движении. Дождитесь окончания "
                    f"или добавьте --ignore-collectors.")
                return 3
        if client is None:
            from shared.ai import client_from_config
            client = client_from_config('dedup', cfg['ai'])

        days = [date.fromisoformat(d) for d in args.day] or None
        logger.info(f"DEDUP_RUN_ONCE | method={method} force={args.force} days={args.day} max_days={args.max_days}")
        res = await run_dedup(pool, client, cfg, method=method, force=args.force, only_days=days,
                              max_days=args.max_days or None)

    st = res.stats
    if res.status != 'ok':
        out(f"Прогон не выполнен ({res.status}): {res.error}")
        return 1
    out(f"Готово: method={method} · дат {st.get('dates_processed')}/{st.get('dates_total')} · запросов {st.get('requests')} "
        f"· групп {st.get('groups_found')} · скрыто {st.get('hidden')} · возвращено {st.get('unhidden')} "
        f"· плохих дат {st.get('bad_days')} · пропущено по лимиту {st.get('skipped_days')}")
    if not args.no_alert and cfg.get('service', {}).get('alerts', True):
        text = notify.run_report(res, method)
        if text:
            await (send or notify.send_admin)(text)
    return 0


async def main(argv: list[str] | None = None) -> int:
    from db.connection import close_db_pool, init_db_pool
    from shared.config import config
    from shared.logging import setup_logging

    args = build_parser().parse_args(argv)
    setup_logging('dedup_run_once')
    pool = await init_db_pool()
    try:
        return await run_once(pool, config['deduplication'], args)
    finally:
        await close_db_pool(pool)


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
