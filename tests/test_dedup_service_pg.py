"""Сервис дедупликатора и журнал прогонов коллекторов на настоящем Postgres (нужна DEDUP_TEST_DSN)."""
import asyncio
import json
import os
from datetime import date, timedelta

import asyncpg
import httpx
import pytest

from deduplicator import main as svc
from deduplicator.schedule import ServiceSettings, close_interrupted_runs, load_snapshot, reap_stale
from shared.ai import AIClient
from shared.run_tracker import track_run

DSN = os.getenv("DEDUP_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="нужна DEDUP_TEST_DSN (Postgres со схемой проекта)")
DAY = date.today() + timedelta(days=5)
ST = ServiceSettings(settle_seconds=60)
CFG = {"method": "ai", "ai": {"priority": [], "concurrency": 2}}


@pytest.fixture
async def pool():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    for t in ("dedup_decisions", "dedup_dates", "dedup_runs", "collector_runs", "channels", "messages", "events"):
        await p.execute(f"TRUNCATE {t} RESTART IDENTITY CASCADE")
    yield p
    await p.close()


def client(available=True):
    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(
            {"groups": [{"ids": ["r1", "r2"], "reason": "то же"}]})}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}})
    return AIClient("dedup", "k" if available else "", ["m1"] if available else [],
                    transport=httpx.MockTransport(handler), retry_delay=0)


async def two_posts(pool):
    for i, ch in ((1, 111), (2, 222)):
        await pool.execute(
            "INSERT INTO messages (id, position, channel_id, text, date, published_at) VALUES ($1,0,$2,$3,$4, now())",
            i, ch, "Концерт группы Икс в клубе Y, 20:00 — вход свободный " + str(ch), DAY)


async def finished_collector(pool, minutes_ago=10, service="collector"):
    await pool.execute(
        "INSERT INTO collector_runs (service, started_at, heartbeat_at, finished_at, status) VALUES "
        "($1, now() - make_interval(mins => $2 + 1), now() - make_interval(mins => $2), now() - make_interval(mins => $2), 'ok')",
        service, minutes_ago)


class Outbox:
    def __init__(self):
        self.texts = []

    async def __call__(self, text):
        self.texts.append(text)


# ------------------------------------------------------------------ track_run

async def test_track_run_writes_ok_row_with_heartbeat(pool):
    async with track_run(pool, "collector", heartbeat_seconds=0.05) as rid:
        first = await pool.fetchval("SELECT heartbeat_at FROM collector_runs WHERE id = $1", rid)
        await asyncio.sleep(0.2)
        assert await pool.fetchval("SELECT heartbeat_at FROM collector_runs WHERE id = $1", rid) > first
        assert await pool.fetchval("SELECT finished_at FROM collector_runs WHERE id = $1", rid) is None
    row = await pool.fetchrow("SELECT * FROM collector_runs WHERE id = $1", rid)
    assert row["status"] == "ok" and row["finished_at"] is not None and row["service"] == "collector"


async def test_track_run_marks_error_and_reraises(pool):
    with pytest.raises(ValueError):
        async with track_run(pool, "event_collector", heartbeat_seconds=10) as rid:
            raise ValueError("boom")
    assert await pool.fetchval("SELECT status FROM collector_runs WHERE id = $1", rid) == "error"


async def test_track_run_never_breaks_the_collector_when_db_is_broken():
    class Broken:
        async def fetchval(self, *a):
            raise RuntimeError("db down")

        async def execute(self, *a):
            raise RuntimeError("db down")

    done = []
    async with track_run(Broken(), "collector") as rid:
        done.append(rid)
    assert done == [None]


# ------------------------------------------------------------------ снимок

async def test_snapshot_counts_active_and_closes_dead_runs(pool):
    await pool.execute("INSERT INTO collector_runs (service) VALUES ('collector')")
    await pool.execute("INSERT INTO collector_runs (service, heartbeat_at) VALUES ('event_collector', now() - interval '40 minutes')")
    snap = await load_snapshot(pool, ServiceSettings(stale_heartbeat_minutes=15))
    assert snap.active_collectors == 1 and snap.last_collector_finish is not None     # зависший закрыт и считается завершённым
    assert await pool.fetchval("SELECT status FROM collector_runs WHERE service = 'event_collector'") == "error"
    assert await reap_stale(pool, 15) == 0


async def test_close_interrupted_dedup_runs(pool):
    await pool.execute("INSERT INTO dedup_runs (method) VALUES ('ai')")
    assert await close_interrupted_runs(pool) == 1
    assert await pool.fetchval("SELECT status FROM dedup_runs") == "error"


# ------------------------------------------------------------------ tick

async def test_legacy_method_never_runs(pool):
    await two_posts(pool)
    assert await svc.tick(pool, client(), {**CFG, "method": "legacy"}, ST, send=Outbox()) is None
    assert await pool.fetchval("SELECT count(*) FROM dedup_runs") == 0


async def test_first_run_hides_duplicate_reports_and_then_waits(pool):
    await two_posts(pool)
    out = Outbox()
    res = await svc.tick(pool, client(), CFG, ST, send=out)
    assert res.status == "ok" and res.stats["hidden"] == 1
    assert len(out.texts) == 1 and out.texts[0].startswith("🧩 Дедупликатор: скрыто 1")
    assert await svc.tick(pool, client(), CFG, ST, send=out) is None            # новых данных нет
    assert len(out.texts) == 1


async def test_waits_for_active_collector_and_settle_then_runs_on_new_data(pool):
    await two_posts(pool)
    out = Outbox()
    await svc.tick(pool, client(), CFG, ST, send=out)
    await pool.execute("INSERT INTO collector_runs (service) VALUES ('event_collector')")        # работает
    assert await svc.tick(pool, client(), CFG, ST, send=out) is None
    await pool.execute("UPDATE collector_runs SET finished_at = now(), status = 'ok'")           # только что закончил
    assert await svc.tick(pool, client(), CFG, ST, send=out) is None                              # settle 60 с
    await pool.execute("UPDATE collector_runs SET finished_at = now() - interval '5 minutes'")
    await pool.execute("UPDATE dedup_runs SET started_at = now() - interval '1 hour'")
    assert (await svc.tick(pool, client(), CFG, ST, send=out)).status == "ok"                     # прогон есть, изменений нет → тишина
    assert len(out.texts) == 1


async def test_ai_unavailable_alerts_once_retries_and_reports_recovery(pool):
    await two_posts(pool)
    out = Outbox()
    res = await svc.tick(pool, client(available=False), CFG, ST, send=out)
    assert res.status == "skipped" and out.texts[0].startswith("🔴")
    assert await svc.tick(pool, client(available=False), CFG, ST, send=out) is None               # пауза retry_minutes
    await pool.execute("UPDATE dedup_runs SET started_at = now() - interval '31 minutes'")
    res2 = await svc.tick(pool, client(available=False), CFG, ST, send=out)
    assert res2.status == "skipped" and len(out.texts) == 1                                       # тот же эпизод — без повтора
    await pool.execute("UPDATE dedup_runs SET started_at = now() - interval '31 minutes'")
    res3 = await svc.tick(pool, client(), CFG, ST, send=out)
    assert res3.status == "ok" and out.texts[1].startswith("🟢") and out.texts[2].startswith("🧩")


async def test_alerts_can_be_switched_off(pool):
    await two_posts(pool)
    out = Outbox()
    res = await svc.tick(pool, client(available=False), CFG, ServiceSettings(alerts=False), send=out)
    assert res.status == "skipped" and out.texts == []


async def test_shadow_method_proposes_and_hides_nothing(pool):
    await two_posts(pool)
    out = Outbox()
    res = await svc.tick(pool, client(), {**CFG, "method": "shadow"}, ST, send=out)
    assert res.status == "ok" and await pool.fetchval("SELECT count(*) FROM messages WHERE hidden_by_admin") == 0
    assert await pool.fetchval("SELECT count(*) FROM dedup_decisions WHERE status = 'proposed'") == 1
    assert out.texts[0].startswith("👀 Дедупликатор (shadow)")
