import asyncio
import os
import sys
from difflib import SequenceMatcher
from collections import defaultdict

if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

os.environ.setdefault('APP_ENV', 'local')

from db.connection import init_db_pool, close_db_pool
from db.tables import Tables
from shared.logging import setup_logging
from shared.config import config
from shared.constants import DUPLICATE_QUICK_COMPARE_LENGTH


async def find_and_mark_duplicates(pool, dry_run: bool = False) -> int:
    dedup_cfg = config.get('deduplication', {})
    flag_threshold = dedup_cfg.get('flag_threshold', 0.7)

    if not dry_run:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(f"""
                    UPDATE {Tables.MESSAGES}
                    SET hidden_by_admin = FALSE, duplicate_of = NULL
                    WHERE hidden_by_admin = TRUE
                    AND duplicate_of IS NOT NULL
                """)

    rows = await pool.fetch(f"""
        SELECT surrogate_id, channel_id, text, date
        FROM {Tables.MESSAGES}
        ORDER BY date NULLS LAST, surrogate_id
    """)

    by_date = defaultdict(list)
    for row in rows:
        by_date[str(row['date'])].append(dict(row))

    seen_pairs = set()
    to_flag = []

    for date_key, msgs in by_date.items():
        if len(msgs) < 2:
            continue

        for i in range(len(msgs)):
            for j in range(i + 1, len(msgs)):
                a = msgs[i]
                b = msgs[j]

                if a['channel_id'] == b['channel_id']:
                    continue

                key = tuple(sorted([a['surrogate_id'], b['surrogate_id']]))
                if key in seen_pairs:
                    continue
                seen_pairs.add(key)

                quick = SequenceMatcher(
                    None,
                    a['text'][:DUPLICATE_QUICK_COMPARE_LENGTH],
                    b['text'][:DUPLICATE_QUICK_COMPARE_LENGTH],
                ).ratio()
                if quick < 0.3:
                    continue

                ratio = SequenceMatcher(None, a['text'], b['text']).ratio()
                if ratio < flag_threshold:
                    continue

                keep = a if a['surrogate_id'] < b['surrogate_id'] else b
                hide = b if a['surrogate_id'] < b['surrogate_id'] else a

                to_flag.append({
                    'ratio': round(ratio, 2),
                    'date': date_key,
                    'keep_sid': keep['surrogate_id'],
                    'hide_sid': hide['surrogate_id'],
                    'keep_text': keep['text'][:80],
                    'hide_text': hide['text'][:80],
                })

    if dry_run:
        print(f"Загружено событий: {len(rows)}")
        print(f"Похожих пар (>= {flag_threshold}): {len(to_flag)}")
        for d in to_flag[:10]:
            print(f"  [{d['ratio']}] Дата: {d['date']}")
            print(f"  ОСТАВЛЯЕМ [{d['keep_sid']}]: {d['keep_text']}")
            print(f"  СКРЫВАЕМ  [{d['hide_sid']}]: {d['hide_text']}")
            print()
        if len(to_flag) > 10:
            print(f"  ... и ещё {len(to_flag) - 10} пар")
        print("DRY RUN — изменения не применены.")
        return len(to_flag)

    async with pool.acquire() as conn:
        async with conn.transaction():
            for d in to_flag:
                await conn.execute(f"""
                    UPDATE {Tables.MESSAGES}
                    SET hidden_by_admin = TRUE,
                        duplicate_of = $2
                    WHERE surrogate_id = $1
                """, d['hide_sid'], d['keep_sid'])

    from loguru import logger
    logger.info(
        f"DEDUP_DONE | total={len(rows)} | flagged={len(to_flag)}"
    )

    return len(to_flag)


async def main():
    setup_logging('find_duplicates')

    dry_run = '--dry-run' in sys.argv

    pool = await init_db_pool()
    try:
        count = await find_and_mark_duplicates(pool, dry_run=dry_run)
        print(f"\nПомечено для ревью: {count} событий")
        if count > 0:
            print("Откройте 🔍 Дубли в боте для ревью.")
    finally:
        await close_db_pool(pool)


if __name__ == '__main__':
    asyncio.run(main())
