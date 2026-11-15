"""Журнал решений дедупликатора (audit.py) и отчёт по нему (scripts/dedup_report.py)."""
import gzip
import importlib.util
import json
import os
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from deduplicator import audit as audit_mod
from deduplicator.audit import AuditLog, NullAudit, audit_from_cfg
from deduplicator.engine import plan_day
from deduplicator.grouping import Group, Groups
from deduplicator.priority import PriorityResolver
from deduplicator.records import KIND_EVENT, KIND_MESSAGE, Record

SCRIPTS = Path(__file__).parent.parent / "scripts"
spec = importlib.util.spec_from_file_location("dedup_report", SCRIPTS / "dedup_report.py")
rep = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rep)

DAY = date(2026, 11, 8)
LONG = "Концерт группы Икс в клубе Y, 20:00. " * 3


def msg(i, ch=1, text=LONG, pos=0, tg=500):
    return Record(KIND_MESSAGE, i, DAY, f"ch:{ch}", text, position=pos, tg_id=tg + i,
                  created_at=datetime(2026, 11, 1, tzinfo=timezone.utc))


def ev(i, text="Группа Икс. Клуб Y", url="https://tickets.example/x"):
    return Record(KIND_EVENT, i, DAY, "event:site", text, url=url)


def resolver():
    return PriorityResolver(["@chan_a", "event:site"], {1: "chan_a", 2: "chan_b"})


def read(path):
    return [json.loads(x) for x in Path(path).read_text(encoding="utf-8").splitlines() if x.strip()]


def make_log(tmp_path, **kw):
    return AuditLog(tmp_path / "dedup_audit.jsonl", **kw)


def groups_of(*gs, raw=None, models=None):
    g = Groups(gs)
    g.raw, g.models = list(raw or []), list(models or [])
    return g


def plan_for(records, groups, live=(), method="shadow"):
    return plan_day(DAY, records, groups, list(live), resolver(), method)


# ------------------------------------------------------------------ содержимое журнала

def test_log_day_writes_group_decision_kept_with_full_context(tmp_path):
    post, other, event, digest = msg(1, 1), msg(2, 2), ev(3), msg(4, 2, text="Суббота • Икс • Игрек", pos=2)
    recs = [post, other, event, digest]
    g = Group([post, other, event, digest], "то же событие")
    plan = plan_for(recs, groups_of(g, raw=['{"groups":[]}'], models=["m1"]))
    log = make_log(tmp_path)
    log.log_day(plan, recs, resolver(), run_id=7, model="m1", groups=groups_of(g, raw=['{"groups":[]}'], models=["m1"]),
                asked_ai=True)
    rows = read(log.path)
    kinds = [r["event"] for r in rows]
    assert kinds[0] == "day" and "ai_raw" in kinds and "group" in kinds
    assert kinds.count("decision") == 3
    day = rows[0]
    assert day["records"] == 4 and day["groups"] == 1 and day["decisions"] == 3 and day["run"] == 7

    group = next(r for r in rows if r["event"] == "group")
    roles = [(m["role"], m["id"]) for m in group["members"]]
    assert roles[0] == ("winner", 1)                                   # победитель всегда первым
    assert {r for r in roles[1:]} == {("loser", 2), ("loser", 3), ("loser", 4)}      # строка дайджеста тоже проигрывает
    assert group["reason"] == "то же событие" and group["group"] == "2026-11-08#1"
    winner = group["members"][0]
    assert winner["text"] == LONG and winner["link"] == "https://t.me/chan_a/501" and winner["name"] == "@chan_a"
    ev_member = next(m for m in group["members"] if m["id"] == 3)
    assert ev_member["link"] == "https://tickets.example/x" and ev_member["via"] == "ai_group"

    dec = [r for r in rows if r["event"] == "decision"]
    assert all(d["status"] == "proposed" and d["is_new"] for d in dec)
    assert {d["loser"]["id"] for d in dec} == {2, 3, 4} and all(d["winner"]["id"] == 1 for d in dec)

    assert [r for r in rows if r["event"] == "kept"] == []


def test_kept_reasons_same_source_list_and_exact_copy_via(tmp_path):
    a, b = msg(1, 1), msg(2, 1, text=LONG)                            # две точные копии одного канала
    other = msg(3, 2, text="другой пост про то же событие " * 3)
    big_list = msg(4, 2, text="Афиша недели: " + " ".join(f"{h}:00 событие" for h in range(10, 20)))
    recs = [a, b, other, big_list]
    g = Group([a, other], "дубль")
    plan = plan_for(recs, groups_of(g))
    log = make_log(tmp_path)
    log.log_day(plan, recs, resolver(), run_id=1, model="rules", groups=groups_of(g), asked_ai=True)
    rows = read(log.path)
    vias = {r["loser"]["id"]: r["via"] for r in rows if r["event"] == "decision"}
    assert vias[3] == "ai_group" and vias[2] == "same_source"
    kept = {r["record"]["id"]: r["reason"] for r in rows if r["event"] == "kept"}
    assert kept[4] == "list"


def test_long_text_is_cut_and_marked(tmp_path):
    big = msg(1, 1, text="слово " * 2000)
    other = msg(2, 2, text="слово " * 1500)
    g = Group([big, other], "дубль")
    plan = plan_for([big, other], groups_of(g))
    log = make_log(tmp_path, max_text_chars=500)
    log.log_day(plan, [big, other], resolver(), run_id=1, model="m", groups=groups_of(g), asked_ai=True)
    w = next(r for r in read(log.path) if r["event"] == "group")["members"][0]
    assert len(w["text"]) == 500 and w["truncated"] is True and w["text_len"] == 12000


def test_change_events_for_dropped_and_winner_changed(tmp_path):
    a, b, c = msg(1, 1), msg(2, 2), msg(3, 2, text="Совсем другое событие в другом месте " * 3)
    g = Group([a, b], "дубль")
    plan = plan_for([a, b, c], groups_of(g))
    prev = [
        dict(winner_kind="m", winner_id=1, loser_kind="m", loser_id=2, status="proposed", decided_by="ai", model="m0", reason="было так"),
        dict(winner_kind="m", winner_id=9, loser_kind="m", loser_id=3, status="proposed", decided_by="ai", model="m0", reason="старое"),
        dict(winner_kind="m", winner_id=8, loser_kind="m", loser_id=2, status="reverted", decided_by="ai", model="m0", reason=""),
    ]
    log = make_log(tmp_path)
    log.log_day(plan, [a, b, c], resolver(), run_id=2, model="m1", groups=groups_of(g), asked_ai=True, prev=prev)
    rows = read(log.path)
    ch = [r for r in rows if r["event"] == "change"]
    assert [(r["change"], r["loser"]["id"]) for r in ch] == [("dropped", 3)]      # (1→2) без изменений, reverted не трогаем
    assert next(r for r in rows if r["event"] == "decision")["is_new"] is False


def test_sticky_pair_is_logged_and_is_not_reported_as_dropped(tmp_path):
    from deduplicator.engine import Live
    a, b = msg(1, 1), msg(2, 2)
    b = Record(b.kind, b.id, b.day, b.source, b.text, hidden=True, tg_id=b.tg_id, created_at=b.created_at)
    live = [Live(10, b.key, a.key, "active", reason="одно и то же")]
    plan = plan_for([a, b], Groups(), live, method="ai")
    prev = [dict(winner_kind="m", winner_id=1, loser_kind="m", loser_id=2, status="active", decided_by="ai",
                 model="m0", reason="одно и то же")]
    log = make_log(tmp_path)
    log.log_day(plan, [a, b], resolver(), run_id=3, model="m1", groups=Groups(), asked_ai=True, prev=prev)
    rows = read(log.path)
    assert [r for r in rows if r["event"] == "change"] == []
    st = [r for r in rows if r["event"] == "sticky"]
    assert len(st) == 1 and st[0]["winner"]["id"] == 1 and st[0]["loser"]["id"] == 2 and st[0]["decision_id"] == 10
    assert next(r for r in rows if r["event"] == "day")["sticky"] == 1


def test_active_method_marks_new_and_existing(tmp_path):
    a, b = msg(1, 1), msg(2, 2)
    g = Group([a, b], "дубль")
    plan = plan_for([a, b], groups_of(g), method="ai")
    log = make_log(tmp_path)
    log.log_day(plan, [a, b], resolver(), run_id=1, model="m", groups=groups_of(g), asked_ai=True)
    assert next(r for r in read(log.path) if r["event"] == "decision")["status"] == "active_new"


# ------------------------------------------------------------------ выключатель и надёжность

def test_disabled_or_missing_config_gives_null_audit(tmp_path):
    assert audit_from_cfg(None) is audit_mod._NULL
    assert audit_from_cfg({}) is audit_mod._NULL
    assert audit_from_cfg({"audit": {"enabled": False}}, tmp_path / "x.jsonl") is audit_mod._NULL
    assert not (tmp_path / "x.jsonl").exists()
    n = NullAudit()
    n.log_day(1, 2, 3)                                                  # любые вызовы — пустые
    n.emit("x", a=1)


def test_enabled_config_creates_file_logger(tmp_path):
    cfg = {"audit": {"enabled": True, "max_text_chars": 300, "rotation_mb": 1, "retention_days": 5, "raw_answers": False}}
    a = audit_from_cfg(cfg, tmp_path / "a.jsonl")
    assert a.enabled and a.max_text == 300 and a.raw_answers is False
    a.emit("run_start", run=1)
    assert read(tmp_path / "a.jsonl")[0]["event"] == "run_start"
    assert audit_from_cfg(cfg, tmp_path / "a.jsonl") is a               # кэшируется на путь


def test_raw_answers_can_be_disabled(tmp_path):
    a, b = msg(1, 1), msg(2, 2)
    g = Group([a, b], "дубль")
    plan = plan_for([a, b], groups_of(g))
    log = make_log(tmp_path, raw_answers=False)
    log.log_day(plan, [a, b], resolver(), run_id=1, model="m", groups=groups_of(g, raw=["{}"], models=["m"]), asked_ai=True)
    assert "ai_raw" not in [r["event"] for r in read(log.path)]


def test_write_failure_does_not_raise(tmp_path, monkeypatch):
    log = make_log(tmp_path)

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(log._log, "info", boom)
    log.emit("x", a=1)
    log.emit("y", a=2)                                                  # повторный сбой тоже тихий


def test_unserializable_values_are_stringified(tmp_path):
    log = make_log(tmp_path)
    log.emit("x", when=datetime(2026, 1, 1), s={1, 2}, d=date(2026, 1, 2))
    row = read(log.path)[0]
    assert row["when"].startswith("2026-01-01") and row["d"] == "2026-01-02"


def test_rotation_compresses_and_old_archives_are_pruned(tmp_path):
    log = make_log(tmp_path, retention_days=30)
    old = tmp_path / "dedup_audit.jsonl.5.gz"
    old.write_bytes(gzip.compress(b"{}\n"))
    ancient = time.time() - 100 * 86400
    os.utime(old, (ancient, ancient))
    log.emit("x", a=1)
    log._handler.doRollover()
    log.emit("y", a=2)
    names = sorted(p.name for p in tmp_path.iterdir())
    assert "dedup_audit.jsonl.1.gz" in names and "dedup_audit.jsonl.5.gz" not in names
    assert json.loads(gzip.open(tmp_path / "dedup_audit.jsonl.1.gz", "rt", encoding="utf-8").read())["event"] == "x"
    assert read(log.path)[0]["event"] == "y"


# ------------------------------------------------------------------ конфиг

def test_config_audit_defaults_and_overrides(monkeypatch):
    from shared import config as cfgmod
    oc = {"base_url": "http://x"}
    monkeypatch.delenv("DEDUP_AUDIT", raising=False)
    s = cfgmod._dedup_section({}, oc)["audit"]
    assert s == {"enabled": True, "max_text_chars": 4000, "rotation_mb": 50, "retention_days": 90, "raw_answers": True}
    off = cfgmod._dedup_section({"audit": {"enabled": False, "rotation_mb": 10}}, oc)["audit"]
    assert off["enabled"] is False and off["rotation_mb"] == 10
    monkeypatch.setenv("DEDUP_AUDIT", "1")
    assert cfgmod._dedup_section({"audit": {"enabled": False}}, oc)["audit"]["enabled"] is True
    monkeypatch.setenv("DEDUP_AUDIT", "0")
    assert cfgmod._dedup_section({}, oc)["audit"]["enabled"] is False


# ------------------------------------------------------------------ отчёт

def build_log(tmp_path):
    a, b, ev1 = msg(1, 1), msg(2, 2), ev(3)
    g = Group([a, b, ev1], "то же событие")
    plan = plan_for([a, b, ev1], groups_of(g, raw=['{"groups":[1]}'], models=["m1"]))
    log = make_log(tmp_path)
    log.log_run_start(run_id=5, method="shadow", models=["m1"], resolver=resolver(), settings={}, prompt_version="v3",
                      dates_total=1, dates_ai=1)
    log.log_day(plan, [a, b, ev1], resolver(), run_id=5, model="m1", groups=groups_of(g, raw=['{"groups":[1]}'], models=["m1"]),
                asked_ai=True)
    log.log_bad_output(run_id=5, day="2026-11-09", error="плохой json", raw="{oops", model="m1")
    log.log_run_end(run_id=5, method="shadow", status="ok", error=None, stats=dict(dates_total=1, dates_processed=1, requests=1),
                    models_used={"m1": 1}, seconds=2.5)
    return log.path


def test_report_tables_group_first_winner_and_filters(tmp_path):
    path = build_log(tmp_path)
    events = list(rep.select(rep.iter_events(path), run=5))
    t = rep.build_tables(events, with_raw=True)
    head, rows = t["Группы"]
    assert rows[0][2] == "победитель" and rows[0][5] == 1 and rows[0][8] == LONG
    assert [r[2] for r in rows] == ["победитель", "скрыт", "скрыт"]
    assert t["Решения"][1][0][3] == "ИИ (группа)" and len(t["Решения"][1]) == 2
    assert t["Прогоны"][1][0][0] == 5 and "m1: 1" in t["Прогоны"][1][0][5]
    assert t["Ошибки"][1][0][3] == "bad_output" and t["Ответы ИИ"][1][0][3] == '{"groups":[1]}'
    assert list(rep.select(rep.iter_events(path), run=99)) == []
    assert list(rep.select(rep.iter_events(path), day_from="2026-12-01")) == [
        e for e in rep.iter_events(path) if not e.get("day")]           # события без даты (run_start/run_end) остаются
    assert any(e["event"] == "group" for e in rep.select(rep.iter_events(path), model="m1"))
    assert not any(e["event"] == "group" for e in rep.select(rep.iter_events(path), model="other"))


def test_report_reads_rotated_archives_in_order(tmp_path):
    base = tmp_path / "dedup_audit.jsonl"
    (tmp_path / "dedup_audit.jsonl.2.gz").write_bytes(gzip.compress(b'{"event":"x","n":1}\n'))
    (tmp_path / "dedup_audit.jsonl.1.gz").write_bytes(gzip.compress(b'{"event":"x","n":2}\n'))
    base.write_text('{"event":"x","n":3}\nбитая строка\n', encoding="utf-8")
    assert [e["n"] for e in rep.iter_events(base)] == [1, 2, 3]


def test_report_main_writes_csv_and_xlsx(tmp_path, capsys):
    path = build_log(tmp_path)
    out = tmp_path / "out"
    assert rep.main(["--log", str(path), "--run", "5", "--out-dir", str(out), "--format", "csv"]) == 0
    files = {p.name.split("_", 3)[-1] for p in out.iterdir()}
    assert any(f.endswith("Группы.csv") for f in files) and any(f.endswith("Решения.csv") for f in files)
    first = next(p for p in out.iterdir() if p.name.endswith("Группы.csv"))
    assert "победитель" in first.read_text(encoding="utf-8-sig")
    pytest.importorskip("openpyxl")
    assert rep.main(["--log", str(path), "--run", "5", "--out-dir", str(out), "--format", "xlsx"]) == 0
    assert any(p.suffix == ".xlsx" for p in out.iterdir())
    assert rep.main(["--log", str(tmp_path / "нет.jsonl"), "--out-dir", str(out)]) == 1
