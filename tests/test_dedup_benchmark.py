"""Бенчмарк моделей: чистая логика отчётов, замер задержек с повторами, независимость от решений в БД."""
import asyncio
import csv
import importlib.util
import json
from collections import Counter
from pathlib import Path

import httpx
import pytest

from shared.ai import AIClient

_spec = importlib.util.spec_from_file_location("dedup_benchmark", Path(__file__).resolve().parent.parent / "scripts" / "dedup_benchmark.py")
bench = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bench)


def pair(kind_id, winner_id=1, day="2026-11-08"):
    return dict(day=day, via="ai_group", reason="r", loser_kind="m", loser_id=kind_id, loser_src="@a", loser_text="t",
                winner_kind="m", winner_id=winner_id, winner_src="@w", winner_text="w")


def res(ids, **kw):
    return dict(status="ok", pairs=[pair(i) for i in ids], **kw)


def test_percentiles_and_default_consensus():
    assert bench.pct([], 0.5) == 0 and bench.pct([1, 2, 3, 4], 0.5) == 2 and bench.pct([1, 2, 3, 4], 0.95) == 4
    assert [bench.default_consensus(n) for n in (1, 2, 3, 4, 5)] == [1, 2, 2, 2, 3]


def test_compare_models_without_gold():
    results = {"a": res([1, 2, 3, 4]), "b": res([1, 2, 3]), "c": res([1, 9])}
    cmp = bench.compare_models(results)                                  # консенсус: ≥2 из 3
    assert cmp["threshold"] == 2 and cmp["consensus_size"] == 3
    a, b, c = (cmp["rows"][m] for m in "abc")
    assert (a["in_consensus"], a["recall_consensus"], a["only_this_model"]) == (3, 1.0, 1)
    assert (b["in_consensus"], b["only_this_model"]) == (3, 0)
    assert (c["in_consensus"], c["recall_consensus"], c["only_this_model"]) == (1, 0.333, 1)
    assert a["false_by_gold"] is None and a["precision_low"] is None


def test_compare_models_with_gold_counts_false_pairs_and_recall():
    results = {"a": res([1, 2, 3, 4]), "b": res([1, 2, 3]), "c": res([1, 9])}
    gold = {("m", 4): False, ("m", 9): True}                              # 4 — ложная склейка, 9 — настоящая находка c
    rows = bench.compare_models(results, gold=gold)["rows"]
    assert rows["a"]["false_by_gold"] == 1 and rows["a"]["precision_high"] == 0.75 and rows["a"]["precision_low"] == 0.75
    assert rows["c"]["false_by_gold"] == 0 and rows["c"]["only_unverified"] == 0
    assert rows["c"]["recall_ref"] == 0.5                                 # эталон {1,2,3,9}, c нашла {1,9}
    assert rows["b"]["recall_ref"] == 0.75


def test_unverified_unique_pairs_lower_the_precision_bound():
    results = {"a": res([1, 2]), "b": res([1, 5])}
    rows = bench.compare_models(results, gold={("m", 1): True})["rows"]
    assert rows["a"]["only_unverified"] == 1 and rows["a"]["precision_low"] == 0.5 and rows["a"]["precision_high"] == 1.0


def test_winner_disagreement_counted_for_shared_losers():
    a = dict(status="ok", pairs=[pair(1, winner_id=10)])
    b = dict(status="ok", pairs=[pair(1, winner_id=11)])
    rows = bench.compare_models({"a": a, "b": b})["rows"]
    assert rows["a"]["diff_winner"] == 1 and rows["b"]["diff_winner"] == 1


def test_pairwise_agreement():
    out = bench.pairwise_agreement({"a": res([1, 2, 3]), "b": res([2, 3, 4])})
    assert out[("a", "b")] == 0.5


def test_parse_verdict_and_gold_file(tmp_path):
    assert [bench.parse_verdict(x) for x in ("д", "Да", "н", "нет", "", "?")] == [True, True, False, False, None, None]
    path = tmp_path / "g.csv"
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=bench.REVIEW_FIELDS)
        w.writeheader()
        w.writerow(dict(loser_kind="m", loser_id=7, verdict="д"))
        w.writerow(dict(loser_kind="e", loser_id=8, verdict="н"))
        w.writerow(dict(loser_kind="e", loser_id=9, verdict=""))
    assert bench.load_gold(path) == {("m", 7): True, ("e", 8): False}


def test_review_lists_contested_first_and_keeps_gold(tmp_path):
    results = {"a": res([1, 2]), "b": res([1])}
    cmp = bench.compare_models(results)
    rows = bench.build_review(results, cmp, {("m", 2): True})
    assert [r["loser_id"] for r in rows] == [2, 1] and rows[0]["verdict"] == "д" and rows[1]["support"] == 2


def test_report_from_result_files(tmp_path, capsys):
    for m, ids in (("a", [1, 2, 3]), ("b", [1, 2]), ("c", [1])):
        (tmp_path / f"result_{m}.json").write_text(json.dumps(dict(
            model=m, status="ok", dates=5, requests=3, wall_sec=10, lat_p50=1.5, lat_p95=3, tokens_in=10, tokens_out=5,
            pairs=[pair(i) for i in ids])), encoding="utf-8")
    (tmp_path / "result_d.json").write_text(json.dumps(dict(model="d", status="skipped", error="AI недоступен")), encoding="utf-8")
    bench.build_report(tmp_path, None, None)
    rows = list(csv.DictReader(open(tmp_path / "summary.csv", encoding="utf-8-sig")))
    assert {r["model"] for r in rows} == {"a", "b", "c", "d"}
    assert next(r for r in rows if r["model"] == "d")["status"] == "skipped"
    assert (tmp_path / "pairs_review.csv").exists()


async def test_instrument_measures_latency_and_retries_after_refusal():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, json={"error": "rate"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}],
                                         "usage": {"prompt_tokens": 1, "completion_tokens": 1}})

    client = AIClient("dedup", "k", ["m1"], transport=httpx.MockTransport(handler), retry_delay=0)
    lat, cnt = [], Counter()
    bench.instrument(client, lat, cnt, pauses=(0,))
    assert await client.complete("s", "u") == "ok"
    assert len(lat) == 1 and cnt["failed_requests"] == 1 and cnt["retried_requests"] == 1 and len(calls) == 2


async def test_instrument_gives_up_after_pauses_exhausted():
    from shared.ai import AIUnavailable
    client = AIClient("dedup", "k", ["m1"], transport=httpx.MockTransport(lambda r: httpx.Response(429)), retry_delay=0)
    lat, cnt = [], Counter()
    bench.instrument(client, lat, cnt, pauses=(0, 0))
    with pytest.raises(AIUnavailable):
        await client.complete("s", "u")
    assert cnt["failed_requests"] == 3 and cnt["retried_requests"] == 2 and lat == []
