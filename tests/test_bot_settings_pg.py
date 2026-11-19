"""bot_settings с настоящей БД: upsert, чтение, сброс; SQL-«сегодня» совпадает с belgrade_today() (нужна DEDUP_TEST_DSN)."""
import os

import asyncpg
import pytest

from db import settings
from shared.tz import BELGRADE_TODAY_SQL, belgrade_today

DSN = os.getenv("DEDUP_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="нужна DEDUP_TEST_DSN (Postgres со схемой проекта)")


@pytest.fixture
async def pool():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
    await p.execute("TRUNCATE bot_settings")
    settings.clear_cache()
    yield p
    await p.execute("TRUNCATE bot_settings")
    await p.close()
    settings.clear_cache()


async def test_set_get_overwrite_delete(pool):
    assert await settings.get_setting(pool, settings.KEY_CATEGORY_MODE) is None
    await settings.set_setting(pool, settings.KEY_CATEGORY_MODE, 'window_admin', 42)
    assert await settings.get_category_mode(pool) == 'window_admin'
    first = await settings.get_setting_row(pool, settings.KEY_CATEGORY_MODE)
    await settings.set_setting(pool, settings.KEY_CATEGORY_MODE, 'window', 43)
    row = await settings.get_setting_row(pool, settings.KEY_CATEGORY_MODE)
    assert row['value'] == 'window' and row['updated_by'] == 43 and row['updated_at'] >= first['updated_at']
    assert await pool.fetchval("SELECT count(*) FROM bot_settings") == 1
    await settings.delete_setting(pool, settings.KEY_CATEGORY_MODE)
    assert await settings.get_setting_row(pool, settings.KEY_CATEGORY_MODE) is None


async def test_missing_table_means_cards_not_crash(pool):
    await pool.execute("ALTER TABLE bot_settings RENAME TO bot_settings_x")
    try:
        settings.clear_cache()
        assert await settings.get_category_mode(pool) == 'cards'
    finally:
        await pool.execute("ALTER TABLE bot_settings_x RENAME TO bot_settings")


async def test_sql_today_matches_python_belgrade_today(pool):
    assert await pool.fetchval(f"SELECT {BELGRADE_TODAY_SQL}") == belgrade_today()
