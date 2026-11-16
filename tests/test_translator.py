"""Перевод победителей после дедупликатора: выбор событий, проход перевода, хук, интеграция с тиком дедупликатора."""
import asyncio
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone

import asyncpg
import httpx
import pytest

from deduplicator import main as dedup_main
from deduplicator import store as dedup_store
from deduplicator.runner import RunResult
from deduplicator.schedule import ServiceSettings, Snapshot
from shared.ai import AIClient
from shared.config import _translation_section
from translator import service as tsvc
from translator.select import select_candidates
from translator.service import PassResult, TranslationHook, TranslationSettings, translation_settings
from translator.text import (BadTranslation, build_description_ru, cut_input, escape_md, is_russian,
                             truncate_word_boundary, validate)

DSN = os.getenv("DEDUP_TEST_DSN")
needs_pg = pytest.mark.skipif(not DSN, reason="нужна DEDUP_TEST_DSN (Postgres со схемой проекта)")
TODAY = date.today()
SERBIAN = "Koncert grupe X u klubu Y. Vrata se otvaraju u 20h, ulaz slobodan za sve posetioce."


# ------------------------------------------------------------------ чистые функции

def test_is_russian_detects_language_and_serbian_cyrillic():
    assert is_russian("Концерт группы в клубе. Начало в 20:00, вход свободный.")
    assert not is_russian(SERBIAN)
    assert not is_russian("A night of flamenco, music and acrobatics at the MTS Dvorana")
    assert not is_russian("Концерт ће се одржати у Дому омладине, улаз је слободан")     # сербская кириллица (ће, у)
    assert not is_russian("Џез вече у клубу")
    assert not is_russian("") and not is_russian("12:00 — 14:00")


def test_build_description_ru_truncates_escapes_and_adds_link():
    text = "Слово " * 200
    out = build_description_ru(text, "https://x.rs/e?a=1", 100)
    body, link = out.split("\n\n")
    assert body.endswith("...") and len(body) <= 103
    assert link == "[ссылка на билеты здесь](https://x.rs/e?a=1)"
    assert build_description_ru("a_b *c* [d]", "https://u", 600).startswith("a\\_b \\*c\\* \\[d\\]")
    assert escape_md("`x`") == "\\`x\\`"
    assert truncate_word_boundary("коротко", 50) == "коротко"


def test_cut_input_has_no_ellipsis_and_breaks_on_word():
    assert cut_input("слово " * 50, 20) == "слово слово слово"
    assert cut_input("короткий текст...", 100) == "короткий текст..."


def test_validate_rejects_empty_and_babble():
    with pytest.raises(BadTranslation):
        validate("  ", "text")
    with pytest.raises(BadTranslation):
        validate("я" * 500, "short")
    validate("ок", "x" * 10)                                  # короткие результаты не проверяем на длину
    validate("я" * 500, "x" * 400)


def test_settings_defaults_and_normalisation():
    assert translation_settings({}).method == "off" and not translation_settings({}).enabled
    s = translation_settings({"method": "ON", "sources": ["EfinityRS", " "], "concurrency": 0})
    assert s.method == "on" and s.sources == ("efinityrs",) and s.enabled and s.concurrency == 1
    assert translation_settings({"method": "bogus", "sources": ["a"]}).method == "off"
    assert not translation_settings({"method": "on", "sources": []}).enabled        # пустой список = ничего не переводим


def test_config_section_reads_env_and_sources(monkeypatch):
    monkeypatch.setenv("OPENCODE_TRANSLATE_API_KEY", "k1")
    monkeypatch.setenv("TRANSLATION_METHOD", "shadow")
    c = _translation_section({"method": "on", "sources": "A, b", "models": ["m"]}, {"base_url": "https://gw"})
    assert (c["method"], c["sources"], c["api_key"], c["base_url"]) == ("shadow", ["a", "b"], "k1", "https://gw")
    monkeypatch.delenv("TRANSLATION_METHOD")
    assert _translation_section({}, {"base_url": "u"})["method"] == "off"


def test_dedup_event_text_ignores_translation():
    row = dict(title="T", venue="V", description="Original text", description_ru="Перевод")
    assert "Перевод" not in dedup_store._event_text(row) and "Original text" in dedup_store._event_text(row)


# ------------------------------------------------------------------ выбор победителей (Postgres)

@pytest.fixture
async def pool():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    for t in ("dedup_decisions", "dedup_dates", "dedup_runs", "events"):
        await p.execute(f"TRUNCATE {t} RESTART IDENTITY CASCADE")
    yield p
    await p.close()


async def add_event(pool, n, *, source="efinityrs", day=None, desc=SERBIAN, desc_ru=None, hidden=False):
    return await pool.fetchval(
        "INSERT INTO events (source, source_url, title, description, description_ru, starts_at, hidden_by_admin) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id",
        source, f"https://x.rs/{n}", f"Event {n}", desc, desc_ru, day or TODAY + timedelta(days=5), hidden)


async def processed(pool, day, *, ago_minutes=-5):
    """Дата обработана дедупликатором (по умолчанию — через 5 минут после создания записей)."""
    await pool.execute(
        "INSERT INTO dedup_dates (day, fingerprint, processed_at) VALUES ($1, 'fp', now() - make_interval(mins => $2::int)) "
        "ON CONFLICT (day) DO UPDATE SET processed_at = EXCLUDED.processed_at", day, ago_minutes)


async def decide(pool, loser_id, status):
    await pool.execute(
        "INSERT INTO dedup_decisions (day, winner_kind, winner_id, loser_kind, loser_id, status) "
        "VALUES ($1, 'm', 1, 'e', $2, $3)", TODAY + timedelta(days=5), loser_id, status)


@needs_pg
async def test_select_only_winners_from_listed_sources(pool):
    d5, d6, d7 = (TODAY + timedelta(days=i) for i in (5, 6, 7))
    await processed(pool, d5)
    ok = await add_event(pool, 1)                                                  # годится
    ok_upper = await add_event(pool, 2, source="EfinityRS")                        # регистр источника не важен
    reverted = await add_event(pool, 3)                                            # админ вернул — победитель
    await decide(pool, reverted, "reverted")
    other_src = await add_event(pool, 4, source="polako")                          # не из списка
    hidden = await add_event(pool, 5, hidden=True)
    proposed = await add_event(pool, 6)
    await decide(pool, proposed, "proposed")
    active = await add_event(pool, 7)
    await decide(pool, active, "active")
    await add_event(pool, 8, desc=None)
    await add_event(pool, 9, desc="   ")
    await add_event(pool, 10, desc_ru="уже переведено")
    await add_event(pool, 11, day=d6)                                              # дата не обработана дедупом
    await processed(pool, d7, ago_minutes=60)                                      # обработана ДО появления события
    await add_event(pool, 12, day=d7)
    past = TODAY - timedelta(days=1)
    await processed(pool, past)
    past_id = await add_event(pool, 13, day=past)

    rows = await select_candidates(pool, ["efinityrs"], only_upcoming=True)
    assert {r["id"] for r in rows} == {ok, ok_upper, reverted}
    rows_all = await select_candidates(pool, ["efinityrs"], only_upcoming=False)
    assert {r["id"] for r in rows_all} == {ok, ok_upper, reverted, past_id}
    assert [r["id"] for r in rows_all][0] == past_id                               # ближайшие даты первыми
    assert await select_candidates(pool, []) == []
    assert {r["id"] for r in await select_candidates(pool, ["polako"])} == {other_src}


# ------------------------------------------------------------------ проход перевода

def make_client(handler):
    return AIClient("translate", "k", ["m1"], transport=httpx.MockTransport(handler), retry_delay=0)


def ok_handler(calls):
    def handler(request):
        calls.append(request.content.decode("utf-8"))
        return httpx.Response(200, json={"choices": [{"message": {"content": "Концерт группы X в клубе Y."}}],
                                         "usage": {"prompt_tokens": 7, "completion_tokens": 3}})
    return handler


ST_ON = TranslationSettings(method="on", sources=("efinityrs",), concurrency=2)


@needs_pg
async def test_run_pass_translates_serbian_and_ignores_russian(pool):
    await processed(pool, TODAY + timedelta(days=5))
    sr = await add_event(pool, 1)
    ru = await add_event(pool, 2, desc="Концерт группы в клубе. Начало в 20:00, вход свободный для всех.")
    calls = []
    res = await tsvc.run_pass(pool, make_client(ok_handler(calls)), ST_ON)
    assert (res.status, res.candidates, res.translated, res.failed) == ("ok", 1, 1, 0)       # русское описание — не кандидат
    assert len(calls) == 1 and "Koncert" in calls[0]                               # модель видела только сербский текст
    got = {r["id"]: r["description_ru"] for r in await pool.fetch("SELECT id, description_ru FROM events")}
    assert got[sr].startswith("Концерт группы X в клубе Y.") and got[sr].endswith("(https://x.rs/1)")
    assert got[ru] is None                                                          # русское: description_ru не пишем
    again = await tsvc.run_pass(pool, make_client(ok_handler(calls)), ST_ON)       # повтор ничего не делает
    assert again.candidates == 0 and len(calls) == 1


@needs_pg
async def test_run_pass_shadow_and_off_do_not_call_model_or_write(pool):
    await processed(pool, TODAY + timedelta(days=5))
    await add_event(pool, 1)
    calls = []
    shadow = await tsvc.run_pass(pool, make_client(ok_handler(calls)), TranslationSettings(method="shadow", sources=("efinityrs",)))
    off = await tsvc.run_pass(pool, make_client(ok_handler(calls)), TranslationSettings(method="off", sources=("efinityrs",)))
    assert shadow.candidates == 1 and shadow.translated == 0 and off.status == "off" and not calls
    assert await pool.fetchval("SELECT count(*) FROM events WHERE description_ru IS NOT NULL") == 0


@needs_pg
async def test_run_pass_stops_when_models_exhausted_and_keeps_rest(pool):
    await processed(pool, TODAY + timedelta(days=5))
    for i in range(1, 4):
        await add_event(pool, i)
    res = await tsvc.run_pass(pool, make_client(lambda r: httpx.Response(429, json={})), ST_ON)
    assert res.status == "incomplete" and res.translated == 0 and res.remaining == 3
    assert res.error                                                                # причина попадёт в сообщение админу
    assert await pool.fetchval("SELECT count(*) FROM events WHERE description_ru IS NULL") == 3


@needs_pg
async def test_run_pass_rejects_babble_and_survives_description_change(pool):
    await processed(pool, TODAY + timedelta(days=5))
    babble = await add_event(pool, 1, desc="Kratko.")
    res = await tsvc.run_pass(pool, make_client(lambda r: httpx.Response(
        200, json={"choices": [{"message": {"content": "я" * 400}}]})), ST_ON)
    assert res.failed == 1 and res.translated == 0
    assert await pool.fetchval("SELECT description_ru FROM events WHERE id=$1", babble) is None

    changed = await add_event(pool, 2)
    client = make_client(ok_handler([]))
    orig = client.complete

    async def complete_and_change(system, user):
        out = await orig(system, user)
        await pool.execute("UPDATE events SET description = 'Novi opis' WHERE id = $1", changed)    # собрали заново во время перевода
        return out

    client.complete = complete_and_change
    res = await tsvc.run_pass(pool, client, TranslationSettings(method="on", sources=("efinityrs",), concurrency=1))
    assert res.stale == 1
    assert await pool.fetchval("SELECT description_ru FROM events WHERE id=$1", changed) is None


# ------------------------------------------------------------------ хук и тик дедупликатора (без БД)

async def test_hook_never_overlaps_and_runs_again_after_busy(monkeypatch):
    gate, started = asyncio.Event(), []

    async def fake_pass(pool, client, st, **kw):
        started.append(1)
        await gate.wait()
        return PassResult(status="ok")

    monkeypatch.setattr(tsvc, "run_pass", fake_pass)
    hook = TranslationHook(None, None, ST_ON)
    assert hook.trigger("a") is True
    await asyncio.sleep(0)
    assert hook.running and hook.trigger("b") is False                              # занято: запомнили, не запускаем второй
    gate.set()
    for _ in range(5):
        await asyncio.sleep(0)
    assert len(started) == 2                                                        # после первого прохода — ещё один
    await hook.aclose()
    assert TranslationHook(None, None, TranslationSettings()).trigger("x") is False  # выключено — не запускается


async def test_hook_retries_after_failure_but_not_forever(monkeypatch):
    results = iter([PassResult(status="incomplete")] * 10)

    async def fake_pass(*a, **kw):
        return next(results)

    monkeypatch.setattr(tsvc, "run_pass", fake_pass)
    st = TranslationSettings(method="on", sources=("a",), retry_minutes=30, max_retries=2)
    hook = TranslationHook(None, None, st)
    hook.trigger("first")
    await hook._task
    t0 = hook.last_finish
    assert hook.poll(t0 + timedelta(minutes=10)) is False                           # рано
    assert hook.poll(t0 + timedelta(minutes=31)) is True                            # повтор 1
    await hook._task
    assert hook.poll(hook.last_finish + timedelta(minutes=31)) is True              # повтор 2
    await hook._task
    assert hook.poll(hook.last_finish + timedelta(minutes=99)) is False             # лимит повторов исчерпан


async def test_tick_triggers_translation_only_after_successful_dedup(monkeypatch):
    snap = Snapshot(now=datetime.now(timezone.utc))
    monkeypatch.setattr(dedup_main, "load_snapshot", lambda pool, st, now=None: asyncio.sleep(0, snap))
    monkeypatch.setattr(dedup_main, "decide", lambda snap_, st: (True, "test"))
    outcomes = iter([RunResult(status="ok", stats={}), RunResult(status="skipped", error="AI недоступен"),
                     RunResult(status="error", error="boom")])

    async def fake_run(pool, client, cfg, method=None, **kw):
        return next(outcomes)

    monkeypatch.setattr(dedup_main, "run_dedup", fake_run)

    @asynccontextmanager
    async def free_lock(pool):                 # пула здесь нет: блокировка Postgres подменена заглушкой
        yield True

    monkeypatch.setattr(dedup_main, "try_lock", free_lock)
    monkeypatch.setattr(dedup_main.dedup_requests, "has_pending", lambda pool: asyncio.sleep(0, False))   # пула нет: очередь ручных запросов пуста
    fired, sent = [], []

    async def send(text):
        sent.append(text)

    st = ServiceSettings(alerts=False)
    for _ in range(3):
        await dedup_main.tick(None, None, {"method": "shadow"}, st, send=send, on_ok=lambda r: fired.append(r.status))
    assert fired == ["ok"]                                                          # skipped/error — перевода нет
    await dedup_main.tick(None, None, {"method": "legacy"}, st, on_ok=lambda r: fired.append("legacy"))
    assert fired == ["ok"]


# ------------------------------------------------------------------ сообщения админу о сбоях перевода

async def _alerts(statuses, st=None, retries_before=None):
    """Прогоняет хук по цепочке статусов, возвращает отправленные сообщения."""
    sent = []

    async def send(text):
        sent.append(text)

    it = iter(statuses)

    async def fake_pass(*a, **kw):
        status = next(it)
        return PassResult(status=status, error="все модели исчерпаны" if status != "ok" else "", remaining=5, translated=7)

    orig = tsvc.run_pass
    tsvc.run_pass = fake_pass
    try:
        hook = TranslationHook(None, None, st or TranslationSettings(method="on", sources=("a",), max_retries=2), send=send)
        for i, _ in enumerate(statuses):
            hook.retries = min(i, hook.st.max_retries) if retries_before is None else retries_before[i]
            hook.trigger(f"t{i}")
            await hook._task
    finally:
        tsvc.run_pass = orig
    return sent


async def test_alert_once_per_episode_then_exhausted_then_recovery():
    sent = await _alerts(["incomplete", "incomplete", "incomplete", "incomplete", "ok", "ok"])
    assert len(sent) == 3                                                           # 4 сбоя подряд и 2 успеха: только 3 сообщения
    assert sent[0].startswith("🔴") and "все модели исчерпаны" in sent[0] and "Повтор через 30 мин" in sent[0]
    assert sent[1].startswith("🔴") and "повторы исчерпаны" in sent[1]
    assert sent[2].startswith("🟢") and "переведено 7" in sent[2]


async def test_new_episode_after_recovery_alerts_again():
    sent = await _alerts(["error", "ok", "error"], retries_before=[0, 0, 0])
    assert [t[:1] for t in sent] == ["🔴", "🟢", "🔴"]


async def test_alert_skipped_means_config_problem_no_retry_promise():
    sent = await _alerts(["skipped", "skipped"])
    assert len(sent) == 1 and "skipped" in sent[0] and "Автоповтор не предусмотрен" in sent[0]


async def test_no_alerts_in_shadow_or_when_disabled_or_without_sender():
    assert await _alerts(["error"], TranslationSettings(method="shadow", sources=("a",))) == []
    assert await _alerts(["error"], TranslationSettings(method="on", sources=("a",), alerts=False)) == []
    hook = TranslationHook(None, None, TranslationSettings(method="on", sources=("a",)))     # без send
    await hook._alert(PassResult(status="error", error="x"))


async def test_failed_alert_send_does_not_break_hook():
    async def boom(text):
        raise RuntimeError("telegram down")

    hook = TranslationHook(None, None, TranslationSettings(method="on", sources=("a",)), send=boom)
    await hook._alert(PassResult(status="error", error="x"))                                 # не бросает
    assert hook._bad is True


def test_translate_key_fallback_to_legacy_name(monkeypatch):
    monkeypatch.delenv("OPENCODE_TRANSLATE_API_KEY", raising=False)
    monkeypatch.setenv("OPENCODE_API_KEY", "old")
    assert _translation_section({}, {"base_url": "u"})["api_key"] == "old"
    monkeypatch.setenv("OPENCODE_TRANSLATE_API_KEY", "new")
    assert _translation_section({}, {"base_url": "u"})["api_key"] == "new"
