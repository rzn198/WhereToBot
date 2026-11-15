"""Бенчмарк моделей AI-парсинга: консенсус, метрики, повторы при отказе, сохранение разметки, выборка из БД."""
import asyncio
import csv
import importlib.util
import json
import os
from argparse import Namespace
from datetime import date
from pathlib import Path

import httpx
import pytest

from collector.ai import extractor
from shared.ai import AIClient

_spec = importlib.util.spec_from_file_location(
    "ai_parsing_benchmark", Path(__file__).resolve().parent.parent / "scripts" / "ai_parsing_benchmark.py")
bench = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bench)

ALLOWED = ["Концерты", "Кино/Театр", "Игры и развлечения"]


def row(i, ai_date="", ai_cats="", error="", stored_date="", stored_cats="", lat="1.0"):
    return dict(id=i, ai_date=ai_date, ai_cats=ai_cats, error=error, latency=("" if error else lat),
                stored_date=stored_date, stored_cats=stored_cats)


def test_consensus_date_and_cats():
    assert bench.consensus_date(["2026-11-08", "2026-11-08", "2026-11-09"]) == "2026-11-08"
    assert bench.consensus_date(["2026-11-08", "2026-11-09"]) is None            # 1 из 2 — меньше порога (2)
    assert bench.consensus_date(["2026-11-08"]) is None                            # одна модель консенсуса не даёт
    assert bench.consensus_date(["", "", "2026-11-09"]) == ""                      # консенсус «даты нет»
    assert bench.consensus_cats([["Концерты"], ["Концерты", "Кино/Театр"], ["Концерты"]]) == ["Концерты"]
    assert bench.consensus_cats([["Концерты"]]) is None


def test_summarize_model_vs_db_and_consensus():
    a = [row(1, "2026-11-08", "Концерты", stored_date="2026-11-08", stored_cats="Концерты"),
         row(2, "2026-11-09", "Концерты", stored_date="2026-11-08", stored_cats="Концерты"),     # разошлись с БД
         row(3, "", "", stored_date="2026-11-10", stored_cats="Концерты"),                       # AI «нет», в БД есть
         row(4, error="bad_output: дата в прошлом: 2025-11-01 при публикации 2026-11-01")]
    b = [row(1, "2026-11-08", "Концерты"), row(2, "2026-11-09", "Кино/Театр"), row(3, "2026-11-10", "Концерты"), row(4, "2026-11-01")]
    c = [row(1, "2026-11-08", "Концерты"), row(2, "2026-11-09", "Концерты"), row(3, "2026-11-10", "Концерты"), row(4, "2026-11-01")]
    by_id = {}
    for m, rows in (("a", a), ("b", b), ("c", c)):
        for r in rows:
            by_id.setdefault(r["id"], {})[m] = r
    s = bench.summarize_model(a, by_id, "a")
    assert (s["posts"], s["answered"], s["bad_output"], s["year_guard"]) == (4, 3, 1, 1)
    assert (s["date_match_db"], s["both_differ"], s["db_found_ai_none"]) == (1, 1, 1)
    # консенсус по постам 1–3: пост 1 (все согласны), пост 2 (a,b,c дают 11-09 → a согласна), пост 3 (b,c = 11-10, a = нет → расходится)
    assert s["date_vs_consensus_n"] == 3 and s["date_vs_consensus_pct"] == pytest.approx(66.7, abs=0.1)
    assert s["non_event"] == 1


def test_gold_scores_and_db_baseline():
    cmp_rows = [dict(id=1, stored_date="2026-11-08", stored_cats="Концерты", gold_date="2026-11-08", gold_cats="Концерты",
                     **{"m__date": "2026-11-08", "m__cats": "Концерты", "m__error": ""}),
                dict(id=2, stored_date="2026-11-01", stored_cats="Концерты", gold_date="нет", gold_cats="Кино/Театр",
                     **{"m__date": "", "m__cats": "Кино/Театр", "m__error": ""}),
                dict(id=3, stored_date="", stored_cats="", gold_date="", gold_cats="",                      # не размечено
                     **{"m__date": "2026-11-09", "m__cats": "", "m__error": ""})]
    g = bench.gold_scores(cmp_rows, ["m"])
    assert (g["db"]["gold_dates"], g["db"]["gold_date_ok"]) == (2, 1)
    assert (g["m"]["gold_dates"], g["m"]["gold_date_ok"], g["m"]["gold_cats_ok"]) == (2, 2, 2)


def make_client(handler):
    return AIClient("parsing", "k", ["m1"], transport=httpx.MockTransport(handler), retry_delay=0)


async def test_ask_retries_after_429_then_succeeds():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={})
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"date":"2026-11-08","categories":["Концерты"]}'}}]})

    client = make_client(handler)
    counters = bench.Counter()
    res, lat, err = await bench.ask_with_retries(client, extractor, "Концерт 8 ноября", date(2026, 11, 1), ALLOWED, 2000,
                                                 counters, pauses=(0, 0))
    assert err == "" and res.date == date(2026, 11, 8) and res.categories == ["Концерты"]
    assert counters["failed_requests"] == 1 and counters["retried_requests"] == 1


async def test_ask_gives_up_and_reports_bad_output():
    client = make_client(lambda r: httpx.Response(429, json={}))
    counters = bench.Counter()
    res, _, err = await bench.ask_with_retries(client, extractor, "t", date(2026, 11, 1), ALLOWED, 2000, counters, pauses=(0,))
    assert res is None and err.startswith("unavailable") and counters["failed_requests"] == 2

    bad = make_client(lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "не json"}}]}))
    res, _, err = await bench.ask_with_retries(bad, extractor, "t", date(2026, 11, 1), ALLOWED, 2000, bench.Counter(), pauses=(0,))
    assert res is None and err.startswith("bad_output")


async def test_run_model_and_report_keeps_manual_gold(tmp_path):
    def handler(request):
        text = request.content.decode("utf-8")
        cats = '["Концерты"]' if "Концерт" in text else '["Кино/Театр"]'
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"date":"2026-11-08","categories":%s}' % cats}}],
                                         "usage": {"prompt_tokens": 5, "completion_tokens": 2}})

    sample = [dict(id=i, channel_id=1, published="2026-11-01", published_date=date(2026, 11, 1),
                   stored_date="2026-11-08" if i == 1 else "", stored_cats="Концерты", text="Концерт " + str(i),
                   text_full="Концерт " + str(i)) for i in (1, 2)]
    args = Namespace(concurrency=2, max_chars=0)
    out = tmp_path
    for model in ("m1", "m2"):
        rows, meta = await bench.run_model(model, sample, {"max_chars": 2000}, args, extractor, ALLOWED,
                                           lambda m: AIClient("parsing", "k", [m], transport=httpx.MockTransport(handler), retry_delay=0))
        bench.write_csv(out / f"result_{model}.csv", rows, bench.RESULT_FIELDS)
        (out / f"result_{model}.json").write_text(json.dumps(meta), encoding="utf-8")
        assert meta["tokens_in"] == 10 and len(rows) == 2
    bench.write_csv(out / "sample.csv", [{**s, "text": s["text"]} for s in sample], bench.SAMPLE_FIELDS)

    sample_files = bench.load_sample_from_files(out)
    bench.build_report(out, ["m1", "m2"], sample_files, None, {1: "@chan"})
    summary = list(csv.DictReader(open(out / "summary.csv", encoding="utf-8-sig")))
    assert [r["model"] for r in summary] == ["m1", "m2"] and summary[0]["answered"] == "2"
    cmp_rows = list(csv.DictReader(open(out / "comparison.csv", encoding="utf-8-sig")))
    assert cmp_rows[0]["channel"] == "@chan" and cmp_rows[0]["m1__date"] == "2026-11-08"
    assert cmp_rows[1]["db_differs_from_consensus"] == "1"            # в БД даты нет, обе модели дали 2026-11-08

    # пользователь заполнил разметку в comparison.csv — повторный отчёт без --gold её не стирает
    cmp_rows[1]["gold_date"] = "2026-11-08"
    bench.write_csv(out / "comparison.csv", cmp_rows)
    bench.build_report(out, ["m1", "m2"], sample_files, None, {})
    again = list(csv.DictReader(open(out / "comparison.csv", encoding="utf-8-sig")))
    assert again[1]["gold_date"] == "2026-11-08"
    assert "gold" in (out / "report.txt").read_text(encoding="utf-8")


DSN = os.getenv("DEDUP_TEST_DSN")


@pytest.mark.skipif(not DSN, reason="нужна DEDUP_TEST_DSN (Postgres со схемой проекта)")
async def test_load_sample_from_db_is_reproducible(tmp_path):
    import asyncpg
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
    try:
        await pool.execute("TRUNCATE dedup_decisions, dedup_dates, channels, messages RESTART IDENTITY CASCADE")
        for i in range(1, 7):
            await pool.execute(
                "INSERT INTO messages (id, position, channel_id, text, date, published_at, event_types) "
                "VALUES ($1, $2, 7, $3, NULL, now() - make_interval(days => $4::int), ARRAY['Концерты'])",
                i, 0 if i <= 5 else 1, f"Концерт номер {i}", i)             # пост 6 — строка дайджеста (position>0)
        a = Namespace(limit=3, days=10, channel=None, random=False, seed=1, resume=False)
        s1 = await bench.load_sample(pool, a, tmp_path)
        assert len(s1) == 3
        assert {r["text"] for r in s1} == {"Концерт номер 1", "Концерт номер 2", "Концерт номер 3"}      # самые свежие
        for d in ("x", "y"):
            (tmp_path / d).mkdir()
        r = Namespace(limit=3, days=None, channel=None, random=True, seed=5, resume=False)
        r1 = await bench.load_sample(pool, r, tmp_path / "x")
        r2 = await bench.load_sample(pool, r, tmp_path / "y")
        assert [x["id"] for x in r1] == [x["id"] for x in r2]                                      # seed повторяем
        again = await bench.load_sample(pool, Namespace(limit=99, days=None, channel=None, random=False, seed=1, resume=True), tmp_path)
        assert [x["id"] for x in again] == [x["id"] for x in s1]                                   # --resume = та же выборка
    finally:
        await pool.close()
