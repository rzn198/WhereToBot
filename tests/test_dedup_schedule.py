"""Расписание дедупликатора: решение по снимку, сообщения админу."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from deduplicator import notify
from deduplicator.engine import DayPlan, NewDecision
from deduplicator.priority import PriorityResolver
from deduplicator.records import KIND_MESSAGE, Record
from deduplicator.schedule import ServiceSettings, Snapshot, decide, service_settings

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
ST = ServiceSettings()


def snap(**kw):
    return Snapshot(now=NOW, **kw)


def ago(**kw):
    return NOW - timedelta(**kw)


def test_waits_while_collector_is_running():
    go, why = decide(snap(active_collectors=1, last_collector_finish=ago(hours=1), last_run_started=ago(hours=2)), ST)
    assert go is False and "коллекторы" in why


def test_waits_settle_time_after_collectors_finish():
    assert decide(snap(last_collector_finish=ago(seconds=60), last_run_started=ago(hours=2)), ST)[0] is False
    assert decide(snap(last_collector_finish=ago(seconds=200), last_run_started=ago(hours=2)), ST)[0] is True


def test_first_run_goes_even_without_collector_history():
    assert decide(snap(), ST) == (True, "первый прогон")


def test_no_run_without_new_collector_data():
    s = snap(last_collector_finish=ago(hours=3), last_run_started=ago(hours=2), last_run_status="ok")
    assert decide(s, ST)[0] is False


def test_runs_when_collector_finished_after_last_run_started():
    s = snap(last_collector_finish=ago(minutes=10), last_run_started=ago(minutes=30), last_run_status="ok")
    assert decide(s, ST) == (True, "коллекторы принесли новые данные")


def test_retry_after_failure_only_after_pause_and_not_endlessly():
    base = dict(last_collector_finish=ago(hours=5), last_run_status="skipped")
    assert decide(snap(**base, last_run_started=ago(minutes=10), bad_streak=1), ST)[0] is False
    assert decide(snap(**base, last_run_started=ago(minutes=31), bad_streak=1), ST)[0] is True
    assert decide(snap(**base, last_run_started=ago(minutes=31), bad_streak=3), ST)[0] is True
    assert decide(snap(**base, last_run_started=ago(minutes=31), bad_streak=4), ST)[0] is False


def test_new_collector_data_overrides_exhausted_retries():
    s = snap(last_collector_finish=ago(minutes=10), last_run_started=ago(hours=3), last_run_status="error", bad_streak=9)
    assert decide(s, ST)[0] is True


def test_service_settings_defaults_and_overrides():
    assert service_settings({}) == ServiceSettings()
    s = service_settings({"service": {"settle_seconds": 5, "retry_minutes": 10, "max_retries": 1, "alerts": False}})
    assert (s.settle_seconds, s.retry_minutes, s.max_retries, s.alerts) == (5, 10, 1, False)


# ------------------------------------------------------------------ сообщения

def rec(i, ch, text="Концерт Икс в клубе Y"):
    return Record(KIND_MESSAGE, i, NOW.date(), f"ch:{ch}", text)


def result(hidden=0, unhidden=0, plans=(), **extra):
    stats = dict(hidden=hidden, unhidden=unhidden, dates_processed=3, dates_total=3, requests=3, tokens_in=10, tokens_out=5)
    stats.update(extra)
    return SimpleNamespace(stats=stats, plans=list(plans), resolver=PriorityResolver(["@a"], {1: "a"}), status="ok", error="")


def test_transition_alerts_once_per_episode():
    assert notify.transition_text("ok", "skipped", "AI недоступен", ST).startswith("🔴")
    assert notify.transition_text(None, "error", "boom", ST).startswith("🔴")
    assert notify.transition_text("skipped", "skipped", "AI недоступен", ST) is None
    assert notify.transition_text("skipped", "error", "x", ST) is None
    assert notify.transition_text("skipped", "ok", "", ST).startswith("🟢")
    assert notify.transition_text("ok", "ok", "", ST) is None


def test_report_ai_lists_samples_and_is_silent_without_changes():
    w, l = rec(1, 1), rec(2, 2, "Потерявший пост " + "очень длинный " * 20)
    plan = DayPlan(day=NOW.date(), method="ai", insert=[NewDecision(l, w, "то же")], hide=[l.key])
    text = notify.run_report(result(hidden=1, plans=[plan]), "ai")
    assert text.startswith("🧩 Дедупликатор: скрыто 1, возвращено 0") and "• @a ← ch:2: Потерявший пост" in text and "…" in text
    assert notify.run_report(result(), "ai") is None


def test_report_warns_about_bad_days_and_budget_even_without_changes():
    text = notify.run_report(result(bad_days=2, skipped_days=5), "ai")
    assert "плохих ответов ИИ: 2" in text and "пропущено дат 5" in text


def test_report_shadow_counts_proposals_only():
    w, l = rec(1, 1), rec(2, 2)
    plan = DayPlan(day=NOW.date(), method="shadow", wanted=[NewDecision(l, w, "то же")])
    text = notify.run_report(result(plans=[plan]), "shadow")
    assert text.startswith("👀 Дедупликатор (shadow): предложено скрыть 1, ничего не скрыто")
    assert notify.run_report(result(), "shadow") is None
