"""Применение SQL-миграций из папки migrations/ (запускается при деплое и вручную).

    python -m db.migrate              применить все новые миграции
    python -m db.migrate --status     показать, что применено и что ждёт
    python -m db.migrate --dry-run    показать, что было бы применено, ничего не меняя

Правила:
  * Файл называется NNN_описание.sql (NNN — номер, миграции применяются по возрастанию).
  * Применённые версии записываются в таблицу schema_migrations и повторно не запускаются.
  * Миграции с номером <= BASELINE_VERSION (001-006) считаются применёнными вручную и
    пропускаются: они выполнялись на проде до появления этого раннера.
  * По умолчанию файл выполняется целиком в одной транзакции (упала любая команда -
    откатывается весь файл, версия не записывается). Не вставляйте в файл BEGIN/COMMIT.
  * Если нужен VACUUM или CREATE INDEX CONCURRENTLY (в транзакции они не работают),
    первой строкой файла напишите `-- migrate: no-transaction`. Тогда команды идут по одной
    (разделитель ';', без $$-блоков), и файл обязан быть безопасным при повторном запуске.
  * Параллельные запуски безопасны: раннер берёт advisory-блокировку Postgres.
  * Код выхода 0 - успех, 1 - ошибка. В docker-compose остальные сервисы ждут успеха.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = ROOT / 'migrations'
BASELINE_VERSION = 6          # 001-006 выполнены вручную до появления раннера
LOCK_KEY = 727450             # произвольный ключ advisory-блокировки

_FILE_RE = re.compile(r'^(\d{3,})_[\w.\-]+\.sql$')
_NO_TX_RE = re.compile(r'^\s*--\s*migrate:\s*no-transaction\s*$', re.I | re.M)

CREATE_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS schema_migrations (
        version     INT          PRIMARY KEY,
        name        TEXT         NOT NULL,
        checksum    TEXT         NOT NULL,
        applied_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
    )
"""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str
    checksum: str
    transactional: bool


def _checksum(sql: str) -> str:
    return hashlib.sha256(sql.replace('\r\n', '\n').encode('utf-8')).hexdigest()


def split_statements(sql: str) -> list[str]:
    """Для режима no-transaction: убирает строки-комментарии и режет по ';'."""
    body = '\n'.join(l for l in sql.splitlines() if not l.lstrip().startswith('--'))
    return [s.strip() for s in body.split(';') if s.strip()]


def discover(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    """Все миграции из папки по возрастанию номера. Два файла с одним номером - ошибка."""
    found: dict[int, Migration] = {}
    for path in sorted(directory.glob('*.sql')):
        m = _FILE_RE.match(path.name)
        if not m:
            continue
        version = int(m.group(1))
        if version in found:
            raise ValueError(f"Две миграции с номером {version:03d}: {found[version].name} и {path.name}")
        sql = path.read_text(encoding='utf-8')
        found[version] = Migration(
            version=version, name=path.name, sql=sql,
            checksum=_checksum(sql), transactional=not _NO_TX_RE.search(sql),
        )
    return [found[v] for v in sorted(found)]


async def _fetch_applied(conn) -> dict[int, tuple[str, str]]:
    rows = await conn.fetch("SELECT version, name, checksum FROM schema_migrations")
    return {r['version']: (r['name'], r['checksum']) for r in rows}


async def _record(conn, m: Migration) -> None:
    await conn.execute(
        "INSERT INTO schema_migrations (version, name, checksum) VALUES ($1, $2, $3)",
        m.version, m.name, m.checksum,
    )


async def _apply(conn, m: Migration) -> None:
    if m.transactional:
        async with conn.transaction():
            await conn.execute(m.sql)
            await _record(conn, m)
    else:
        for statement in split_statements(m.sql):
            await conn.execute(statement)
        await _record(conn, m)


async def migrate(
    conn,
    migrations: list[Migration],
    baseline: int = BASELINE_VERSION,
    dry_run: bool = False,
) -> list[Migration]:
    """Применяет новые миграции. Возвращает применённые (при dry_run - те, что были бы применены).
    Первая же ошибка прерывает процесс исключением: следующие миграции не запускаются."""
    await conn.execute("SELECT pg_advisory_lock($1)", LOCK_KEY)
    try:
        await conn.execute(CREATE_TABLE_SQL)
        applied = await _fetch_applied(conn)

        for m in migrations:
            if m.version in applied and applied[m.version][1] != m.checksum:
                logger.warning(
                    f"MIGRATE_CHANGED | {m.name} изменён после применения "
                    f"(применённая версия сохранена, повторно не запускается)"
                )

        pending = [m for m in migrations if m.version > baseline and m.version not in applied]
        if not pending:
            logger.info("MIGRATE | новых миграций нет")
            return []

        done: list[Migration] = []
        for m in pending:
            if dry_run:
                logger.info(f"MIGRATE_DRY_RUN | {m.name}")
            else:
                logger.info(f"MIGRATE_APPLY | {m.name} | transactional={m.transactional}")
                await _apply(conn, m)
                logger.info(f"MIGRATE_APPLIED | {m.name}")
            done.append(m)
        return done
    finally:
        await conn.execute("SELECT pg_advisory_unlock($1)", LOCK_KEY)


async def status(conn, migrations: list[Migration], baseline: int = BASELINE_VERSION) -> list[tuple[str, str]]:
    await conn.execute(CREATE_TABLE_SQL)
    applied = await _fetch_applied(conn)
    rows = []
    for m in migrations:
        if m.version in applied:
            state = 'применена'
        elif m.version <= baseline:
            state = 'пропущена (выполнена вручную до раннера)'
        else:
            state = 'ЖДЁТ ПРИМЕНЕНИЯ'
        rows.append((m.name, state))
    return rows


async def _connect():
    import asyncpg
    from shared.config import config
    db = config['db']
    logger.info(f"MIGRATE_DB | {db['host']}:{db['port']}/{db['name']} (APP_ENV={config['env']})")
    return await asyncpg.connect(
        host=db['host'], port=db['port'], database=db['name'],
        user=db['user'], password=db['password'],
    )


async def _main(args) -> int:
    try:
        migrations = discover()
        conn = await _connect()
    except Exception as e:
        logger.error(f"MIGRATE_FAILED | {type(e).__name__}: {e}")
        print(f"ОШИБКА МИГРАЦИИ (подготовка): {e}", file=sys.stderr)
        return 1
    try:
        if args.status:
            for name, state in await status(conn, migrations):
                print(f"{name:<50} {state}")
            return 0
        applied = await migrate(conn, migrations, dry_run=args.dry_run)
        print(f"{'Было бы применено' if args.dry_run else 'Применено'} миграций: {len(applied)}")
        for m in applied:
            print(f"  {m.name}")
        return 0
    except Exception as e:
        logger.error(f"MIGRATE_FAILED | {type(e).__name__}: {e}")
        print(f"ОШИБКА МИГРАЦИИ: {e}", file=sys.stderr)
        return 1
    finally:
        await conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='python -m db.migrate', description=__doc__.split('\n')[0])
    parser.add_argument('--status', action='store_true', help='показать состояние миграций')
    parser.add_argument('--dry-run', action='store_true', help='ничего не применять, только показать')
    return asyncio.run(_main(parser.parse_args(argv)))


if __name__ == '__main__':
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    sys.exit(main())
