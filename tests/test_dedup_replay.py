import importlib.util
import sys
from datetime import date
from pathlib import Path

from deduplicator.engine import DayPlan, NewDecision
from deduplicator.grouping import Group
from deduplicator.priority import PriorityResolver
from deduplicator.records import KIND_EVENT, KIND_MESSAGE, Record

SCRIPTS = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("dedup_replay", SCRIPTS / "dedup_replay.py")
rep = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rep)

DAY = date(2026, 11, 8)


def m(i, ch=1, pos=0, text="Концерт Икс"):
    return Record(KIND_MESSAGE, i, DAY, f"ch:{ch}", text, position=pos)


def e(i, text="Икс"):
    return Record(KIND_EVENT, i, DAY, "event:site", text)


def make_plan():
    post, ev, same, digest = m(1), e(2), m(3), m(4, ch=2, pos=2, text="Суббота • Икс • Игрек")
    g = Group([post, ev, same, digest], "то же событие")
    plan = DayPlan(day=DAY, method="shadow", groups=[g], wanted=[NewDecision(ev, post, "то же событие")])
    return plan, (post, ev, same, digest)


def test_cluster_rows_roles_labels_and_legacy_marks():
    plan, (post, ev, same, digest) = make_plan()
    r = PriorityResolver(["@chan_a"], {1: "chan_a"})
    rows = rep.cluster_rows([plan], r, {ev.key: "старый хук"}, "model-x")
    by_id = {x["id"]: x for x in rows}
    assert by_id[1]["role"] == "winner" and by_id[1]["reason"] == "то же событие" and by_id[1]["source"] == "@chan_a"
    assert by_id[2]["role"] == "loser" and by_id[2]["kind"] == "event" and by_id[2]["legacy"] == "старый хук"
    assert by_id[3]["role"] == "kept (тот же источник)" and by_id[4]["role"] == "kept (дайджест)"
    assert {x["model"] for x in rows} == {"model-x"} and by_id[1]["group"] == "2026-11-08#1"


def test_cluster_rows_keep_full_text_and_winner_first():
    plan, (post, *_rest) = make_plan()
    long_text = "слово\nещё слово " * 300
    plan.groups[0].members[0] = Record(KIND_MESSAGE, 1, DAY, "ch:1", long_text)
    rows = rep.cluster_rows([plan], PriorityResolver(["@x"], {}), {}, "x")
    assert any(x["text"] == long_text.strip() for x in rows)           # полный текст, с переносами строк
    assert rows[0]["role"] == "winner"


def test_loser_pairs_and_legacy_overlap():
    plan, (post, ev, *_rest) = make_plan()
    assert rep.loser_pairs([plan]) == {(ev.key, post.key)}
    legacy = {ev.key: "старый хук", ("m", 99): "старый хук", ("m", 98): "скрыт вручную"}
    assert rep.legacy_hidden([plan], legacy) == (2, 1)


def test_save_csv_roundtrip(tmp_path):
    plan, _ = make_plan()
    rows = rep.cluster_rows([plan], PriorityResolver(), {}, "x")
    path = tmp_path / "c.csv"
    rep.save_csv(path, rows)
    import csv
    back = list(csv.DictReader(open(path, encoding="utf-8-sig")))
    assert len(back) == 4 and list(back[0].keys()) == rep.FIELDS


def test_cluster_rows_add_exact_copies_found_by_engine_and_digest_not_winner():
    win, lose, copy, digest = m(1, text="Длинный анонс события для проверки"), m(2, ch=2, text="Длинный анонс события для проверки"), \
        m(3, ch=2, text="Длинный анонс события для проверки"), m(4, ch=3, pos=4, text="Суббота • Икс")
    g = Group([digest, lose, win], "то же")
    plan = DayPlan(day=DAY, method="shadow", groups=[g],
                   wanted=[NewDecision(lose, win, "то же"), NewDecision(copy, win, "то же")])
    rows = rep.cluster_rows([plan], PriorityResolver(["ch:3"]), {}, "x")     # дайджест в приоритете, но победить не может
    assert [(x["id"], x["role"]) for x in rows] == [(1, "winner"), (4, "kept (дайджест)"), (2, "loser"), (3, "loser")]
