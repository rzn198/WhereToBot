"""Журнал решений на настоящем Postgres: прогон run_dedup пишет события и сверяется с таблицами (нужна DEDUP_TEST_DSN)."""
import json
import os
from datetime import date

import asyncpg
import httpx
import pytest

from deduplicator import audit as audit_mod
from deduplicator.runner import run_dedup
from shared.ai import AIClient

DSN = os.getenv("DEDUP_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="нужна DEDUP_TEST_DSN (Postgres со схемой проекта)")

DAY = date(2026, 11, 8)


@pytest.fixture
async def pool():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    for t in ("dedup_decisions", "dedup_dates", "dedup_runs", "collector_runs", "channels", "messages", "events"):
        await p.execute(f"TRUNCATE {t} RESTART IDENTITY CASCADE")
    yield p
    await p.close()


def client(answer):
    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": answer}}],
                                         "usage": {"prompt_tokens": 1, "completion_tokens": 1}})
    return AIClient("dedup", "k", ["m1"], transport=httpx.MockTransport(handler), retry_delay=0)


async def seed(pool):
    await pool.execute("INSERT INTO channels (channel_id, username) VALUES (111, 'chan_a'), (222, 'chan_b')")
    for i, ch in ((11, 111), (12, 222)):
        await pool.execute(
            "INSERT INTO messages (id, position, channel_id, text, date, published_at) VALUES ($1,0,$2,$3,$4, now())",
            i, ch, "Концерт группы Икс в клубе Y, 20:00 — вход свободный " + str(ch), DAY)


def cfg(enabled=True):
    return {"method": "shadow", "audit": {"enabled": enabled, "max_text_chars": 4000, "rotation_mb": 50,
                                          "retention_days": 90, "raw_answers": True},
            "ai": {"priority": ["@chan_a"], "concurrency": 2, "horizon_days": 3650}}


def read(path):
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


async def test_run_writes_full_journal_and_records_per_day_model(pool, tmp_path, monkeypatch):
    monkeypatch.setattr(audit_mod, "default_path", lambda: tmp_path / "dedup_audit.jsonl")
    await seed(pool)
    res = await run_dedup(pool, client(json.dumps({"groups": [{"ids": ["r1", "r2"], "reason": "то же"}]})), cfg(),
                          method="shadow", start_date=DAY)
    assert res.status == "ok"
    rows = read(tmp_path / "dedup_audit.jsonl")
    kinds = [r["event"] for r in rows]
    assert kinds[0] == "run_start" and kinds[-1] == "run_end"
    assert {"day", "ai_raw", "group", "decision"} <= set(kinds)
    start, end = rows[0], rows[-1]
    assert start["run"] == res.run_id == end["run"] and start["models"] == ["m1"] and start["priority"] == ["@chan_a"]
    assert end["status"] == "ok" and end["models_used"] == {"m1": 1} and end["stats"]["groups_found"] == 1
    dec = next(r for r in rows if r["event"] == "decision")
    assert dec["winner"]["name"] == "@chan_a" and dec["loser"]["name"] == "@chan_b" and dec["status"] == "proposed"
    assert dec["winner"]["link"] == "https://t.me/chan_a/11"
    assert "Концерт группы Икс" in dec["loser"]["text"]
    db = await pool.fetchrow("SELECT model, status, reason FROM dedup_decisions")
    assert (db["model"], db["status"], db["reason"]) == ("m1", "proposed", "то же")

    # второй прогон с другим ответом: прежнее решение должно попасть в change
    await pool.execute("DELETE FROM dedup_dates")
    await run_dedup(pool, client(json.dumps({"groups": []})), cfg(), method="shadow", start_date=DAY)
    rows2 = read(tmp_path / "dedup_audit.jsonl")
    changes = [r for r in rows2 if r["event"] == "change"]
    assert [c["change"] for c in changes] == ["dropped"] and changes[0]["loser"]["name"] == "@chan_b"


async def test_bad_answer_is_journaled_and_date_not_applied(pool, tmp_path, monkeypatch):
    monkeypatch.setattr(audit_mod, "default_path", lambda: tmp_path / "bad.jsonl")
    await seed(pool)
    res = await run_dedup(pool, client("это не json"), cfg(), method="shadow", start_date=DAY)
    assert res.stats["bad_days"] == 1
    rows = read(tmp_path / "bad.jsonl")
    bad = next(r for r in rows if r["event"] == "bad_output")
    assert bad["raw"] == "это не json" and bad["model"] == "m1" and bad["day"] == "2026-11-08"
    assert not any(r["event"] in ("group", "decision") for r in rows)


async def test_flag_off_writes_nothing_and_dry_run_never_writes(pool, tmp_path, monkeypatch):
    monkeypatch.setattr(audit_mod, "default_path", lambda: tmp_path / "off.jsonl")
    await seed(pool)
    ans = json.dumps({"groups": [{"ids": ["r1", "r2"], "reason": "то же"}]})
    await run_dedup(pool, client(ans), cfg(enabled=False), method="shadow", start_date=DAY)
    assert not (tmp_path / "off.jsonl").exists()
    await pool.execute("DELETE FROM dedup_dates")
    await run_dedup(pool, client(ans), cfg(enabled=True), method="shadow", start_date=DAY, dry_run=True)
    assert not (tmp_path / "off.jsonl").exists()


async def test_shadow_keeps_winner_and_decision_ids_when_higher_priority_source_joins(pool, tmp_path, monkeypatch):
    monkeypatch.setattr(audit_mod, "default_path", lambda: tmp_path / "dedup_audit.jsonl")
    await seed(pool)
    c = cfg()
    c["ai"]["priority"] = ["@chan_c", "@chan_a", "@chan_b"]
    await run_dedup(pool, client(json.dumps({"groups": [{"ids": ["r1", "r2"], "reason": "то же"}]})), c,
                    method="shadow", start_date=DAY)
    first = await pool.fetch("SELECT id, winner_id, loser_id FROM dedup_decisions WHERE status = 'proposed'")
    assert [(r["winner_id"], r["loser_id"]) for r in first] == [(1, 2)]
    await pool.execute("INSERT INTO channels (channel_id, username) VALUES (333, 'chan_c')")
    await pool.execute(
        "INSERT INTO messages (id, position, channel_id, text, date, published_at) VALUES (13,0,333,$1,$2, now())",
        "Концерт группы Икс в клубе Y, 20:00 — вход свободный 333", DAY)
    await run_dedup(pool, client(json.dumps({"groups": [{"ids": ["r1", "r2", "r3"], "reason": "то же"}]})), c,
                    method="shadow", start_date=DAY)
    rows = await pool.fetch("SELECT id, winner_id, loser_id FROM dedup_decisions WHERE status = 'proposed' ORDER BY loser_id")
    assert [(r["winner_id"], r["loser_id"]) for r in rows] == [(1, 2), (1, 3)]
    assert rows[0]["id"] == first[0]["id"]                                    # решение не пересоздавалось


async def test_big_day_is_asked_for_every_pair_of_parts_and_groups_are_merged(pool, tmp_path, monkeypatch):
    monkeypatch.setattr(audit_mod, "default_path", lambda: tmp_path / "dedup_audit.jsonl")
    for ch in range(101, 110):
        await pool.execute("INSERT INTO channels (channel_id, username) VALUES ($1, $2)", ch, f"chan_{ch}")
        await pool.execute(
            "INSERT INTO messages (id, position, channel_id, text, date, published_at) VALUES ($1,0,$2,$3,$4, now())",
            ch, ch, f"Концерт группы Икс {ch} в клубе Y, 20:00 — вход свободный", DAY)
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(
            {"groups": [{"ids": ["r1", "r2"], "reason": "то же"}]})}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}})

    c = cfg()
    c["ai"]["max_records_per_request"] = 4                                    # части по 2 записи → 5 частей → 10 запросов
    ai = AIClient("dedup", "k", ["m1"], transport=httpx.MockTransport(handler), retry_delay=0)
    res = await run_dedup(pool, ai, c, method="shadow", start_date=DAY)
    assert res.status == "ok" and len(calls) == 10 and res.stats["requests"] == 10


async def test_dry_run_with_ignore_decisions_does_not_inherit_prod_winner(pool, tmp_path, monkeypatch):
    monkeypatch.setattr(audit_mod, "default_path", lambda: tmp_path / "dedup_audit.jsonl")
    await seed(pool)
    msgs = await pool.fetch("SELECT surrogate_id, channel_id FROM messages ORDER BY channel_id")
    low, top = msgs[1], msgs[0]                         # приоритет: chan_a (111) выше chan_b (222)
    # на проде «действующим» победителем когда-то стал низкий по приоритету пост
    await pool.execute(
        "INSERT INTO dedup_decisions (day, winner_kind, winner_id, loser_kind, loser_id, status, decided_by) "
        "VALUES ($1,'m',$2,'m',$3,'proposed','ai')", DAY, low["surrogate_id"], top["surrogate_id"])
    answer = json.dumps({"groups": [{"ids": ["r1", "r2"], "reason": "то же"}]})
    kept = await run_dedup(pool, client(answer), cfg(), method="shadow", dry_run=True, force=True, start_date=DAY)
    fresh = await run_dedup(pool, client(answer), cfg(), method="shadow", dry_run=True, force=True, start_date=DAY,
                            ignore_decisions=True)
    assert [(nd.winner.id, nd.loser.id) for p in kept.plans for nd in p.wanted] == [(low["surrogate_id"], top["surrogate_id"])]
    assert [(nd.winner.id, nd.loser.id) for p in fresh.plans for nd in p.wanted] == [(top["surrogate_id"], low["surrogate_id"])]
