"""Чистка пересмотров по дате из конфига и порядок запуска в дедупликаторе; счётчики категорий только у админа."""
from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.handlers import categories as cats
from bot.keyboards.main_menu import get_main_menu_inline
from bot.utils.categories import get_category
from db.review_cleanup import ReviewCleanupJob, ReviewCleanupSettings, purge_reviews, review_cleanup_settings
from deduplicator.main import run_after_jobs

TODAY = date(2026, 11, 14)


# ---------- настройки ----------

@pytest.mark.parametrize("cfg", [None, {}, {"cleanup_before": ""}, {"cleanup_before": None}])
def test_disabled_without_date(cfg):
    assert review_cleanup_settings(cfg, TODAY).before is None


def test_date_formats():
    assert review_cleanup_settings({"cleanup_before": "2026-10-01"}, TODAY).before == date(2026, 10, 1)
    assert review_cleanup_settings({"cleanup_before": date(2026, 10, 1)}, TODAY).before == date(2026, 10, 1)   # YAML без кавычек
    assert review_cleanup_settings({"cleanup_before": datetime(2026, 10, 1, 5)}, TODAY).before == date(2026, 10, 1)


def test_bad_or_future_date_is_ignored():
    assert review_cleanup_settings({"cleanup_before": "вчера"}, TODAY).before is None
    assert review_cleanup_settings({"cleanup_before": "2026-12-01"}, TODAY).before is None
    assert review_cleanup_settings({"cleanup_before": TODAY}, TODAY).before == TODAY


def test_only_resolved_defaults_to_true():
    assert review_cleanup_settings({"cleanup_before": "2026-10-01"}, TODAY).only_resolved is True
    assert review_cleanup_settings({"cleanup_before": "2026-10-01", "only_resolved": None}, TODAY).only_resolved is True
    assert review_cleanup_settings({"cleanup_before": "2026-10-01", "only_resolved": False}, TODAY).only_resolved is False


# ---------- удаление ----------

async def test_purge_sql_only_resolved_vs_all():
    pool = MagicMock(); pool.execute = AsyncMock(return_value="DELETE 3")
    assert await purge_reviews(pool, ReviewCleanupSettings(date(2026, 10, 1), True)) == 3
    sql = pool.execute.await_args.args[0]
    assert "status IN ('resolved', 'applied')" in sql and pool.execute.await_args.args[1] == date(2026, 10, 1)
    await purge_reviews(pool, ReviewCleanupSettings(date(2026, 10, 1), False))
    assert "status" not in pool.execute.await_args.args[0]


async def test_purge_without_date_does_not_touch_db():
    pool = MagicMock(); pool.execute = AsyncMock()
    assert await purge_reviews(pool, ReviewCleanupSettings(None, True)) == 0
    pool.execute.assert_not_awaited()


# ---------- задача ----------

async def test_job_runs_once_per_day_and_again_after_config_change():
    pool = MagicMock(); pool.execute = AsyncMock(return_value="DELETE 2")
    cfg = {"cleanup_before": "2026-10-01"}
    job = ReviewCleanupJob(pool, lambda: cfg)
    assert await job.run_if_due(TODAY) == 2
    assert await job.run_if_due(TODAY) is None                       # в тот же день повторно не идёт
    assert await job.run_if_due(date(2026, 11, 15)) == 2             # на следующий день снова
    cfg["cleanup_before"] = "2026-10-15"
    assert await job.run_if_due(date(2026, 11, 15)) == 2             # изменили дату в конфиге — снова


async def test_job_disabled_and_db_error():
    pool = MagicMock(); pool.execute = AsyncMock()
    assert await ReviewCleanupJob(pool, lambda: {}).run_if_due(TODAY) is None
    pool.execute.assert_not_awaited()
    pool.execute = AsyncMock(side_effect=RuntimeError("db"))
    job = ReviewCleanupJob(pool, lambda: {"cleanup_before": "2026-10-01"})
    assert await job.run_if_due(TODAY) is None
    pool.execute = AsyncMock(return_value="DELETE 0")
    assert await job.run_if_due(TODAY) == 0                          # после сбоя следующая попытка не заблокирована


# ---------- порядок: после остальных задач ----------

class Hook:
    def __init__(self, running): self.running = running


class Job:
    def __init__(self): self.calls = 0
    async def run_if_due(self): self.calls += 1; return 5


async def test_runs_only_when_dedup_and_translation_are_idle():
    job = Job()
    assert await run_after_jobs(None, Hook(False), job) == 5 and job.calls == 1
    assert await run_after_jobs(object(), Hook(False), job) is None       # в этом такте был прогон дедупликации
    assert await run_after_jobs(None, Hook(True), job) is None            # идёт перевод
    assert job.calls == 1


# ---------- счётчики категорий только у админа ----------

def texts(kb):
    return [b.text for r in kb.inline_keyboard for b in r]


COUNTS = {"concerts": 12, "fun": 3, "rest": 40, "theater": 30}


def test_admin_sees_counts_on_all_three_category_buttons():
    t = texts(get_main_menu_inline(is_admin=True, counts=COUNTS))
    assert "🎵 Концерты (12)" in t and "🎲 Игры и развлечения (3)" in t
    rest = [b.text for r in get_main_menu_inline(is_admin=True, counts=COUNTS).inline_keyboard for b in r if b.callback_data == 'cats:menu']
    assert len(rest) == 1 and rest[0].endswith("(40)")


def test_regular_user_never_sees_counts_even_if_passed():
    t = texts(get_main_menu_inline(is_admin=False, counts=COUNTS))
    assert "🎵 Концерты" in t
    assert any(b.callback_data == 'cats:menu' for r in get_main_menu_inline(is_admin=False, counts=COUNTS).inline_keyboard for b in r)
    assert not any("(" in x for x in t)


def test_admin_without_counts_gets_plain_labels():
    assert "🎵 Концерты" in texts(get_main_menu_inline(is_admin=True, counts=None))


def test_rest_list_counts_only_for_admin():
    data = [(get_category("theater"), 30), (get_category("expo"), 5)]
    assert texts(cats.build_menu_keyboard(data))[0] == "🎭 Кино/Театр"
    assert texts(cats.build_menu_keyboard(data, show_counts=True))[0] == "🎭 Кино/Театр (30)"
