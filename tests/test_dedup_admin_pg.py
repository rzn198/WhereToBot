"""Ручные действия админа над решениями, экраны «Скрытые события» и «Дубли», run_once, чистка истории (нужна DEDUP_TEST_DSN)."""
import argparse
import os
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest

from bot.handlers import admin_dedup_ai, admin_duplicates, admin_panel
from db import dedup_admin as da
from db.cleanup import purge_dedup_history
from deduplicator import run_once as ro
from deduplicator.lock import try_lock
from tests.test_dedup_service_pg import CFG, client

DSN = os.getenv("DEDUP_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="нужна DEDUP_TEST_DSN (Postgres со схемой проекта)")
DAY = date.today() + timedelta(days=5)


@pytest.fixture
async def pool():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=5)
    for t in ("dedup_decisions", "dedup_dates", "dedup_runs", "collector_runs", "channels", "messages", "events"):
        await p.execute(f"TRUNCATE {t} RESTART IDENTITY CASCADE")
    yield p
    await p.close()


async def post(pool, i, ch=111, hidden=False, position=0, text=None):
    return await pool.fetchval(
        "INSERT INTO messages (id, position, channel_id, text, date, published_at, hidden_by_admin) "
        "VALUES ($1,$2,$3,$4,$5, now(), $6) RETURNING surrogate_id",
        i, position, ch, text or f"Концерт группы Икс в клубе Y, 20:00 — пост {i}", DAY, hidden)


async def event(pool, url, hidden=False):
    return await pool.fetchval(
        "INSERT INTO events (source, source_url, title, starts_at, city, hidden_by_admin) "
        "VALUES ('biletrs', $1, $1, $2, 'Belgrade', $3) RETURNING id", url, DAY, hidden)


async def decide(pool, winner, loser, status="active", decided_by="ai", reason="то же событие"):
    return await pool.fetchval(
        "INSERT INTO dedup_decisions (day, winner_kind, winner_id, loser_kind, loser_id, status, decided_by, model, reason) "
        "VALUES ($1,$2,$3,$4,$5,$6,$7,'m1',$8) RETURNING id", DAY, winner[0], winner[1], loser[0], loser[1],
        status, decided_by, reason)


async def hidden(pool, kind, rid):
    t, c = ("messages", "surrogate_id") if kind == "m" else ("events", "id")
    return await pool.fetchval(f"SELECT hidden_by_admin FROM {t} WHERE {c} = $1", rid)


async def status(pool, did):
    return await pool.fetchval("SELECT status FROM dedup_decisions WHERE id = $1", did)


# ------------------------------------------------------------------------------------- действия

def test_refs():
    assert da.parse_ref("m12") == ("m", 12) and da.parse_ref("e5") == ("e", 5)
    assert da.parse_ref("x1") is None and da.parse_ref("m") is None and da.parse_ref("") is None


async def test_restore_loser_post_and_event(pool):
    w, l = await post(pool, 1), await post(pool, 2, ch=222, hidden=True)
    d = await decide(pool, ("m", w), ("m", l))
    assert (await da.restore_loser(pool, d)).ok
    assert await hidden(pool, "m", l) is False and await status(pool, d) == "reverted"
    # событие сайта
    e = await event(pool, "http://x", hidden=True)
    d2 = await decide(pool, ("m", w), ("e", e))
    assert (await da.restore_loser(pool, d2)).ok and await hidden(pool, "e", e) is False


async def test_restore_twice_is_safe(pool):
    w, l = await post(pool, 1), await post(pool, 2, ch=222, hidden=True)
    d = await decide(pool, ("m", w), ("m", l))
    await da.restore_loser(pool, d)
    out = await da.restore_loser(pool, d)
    assert not out.ok and "изменилось" in out.text


async def test_swap_with_group_of_three(pool):
    w, l1, l2 = await post(pool, 1), await post(pool, 2, ch=222, hidden=True), await post(pool, 3, ch=333, hidden=True)
    d1, d2 = await decide(pool, ("m", w), ("m", l1)), await decide(pool, ("m", w), ("m", l2))
    assert (await da.swap_decision(pool, d1, admin_id=7)).ok
    assert await hidden(pool, "m", w) is True and await hidden(pool, "m", l1) is False
    assert await status(pool, d1) == "stale"
    # второй проигравший перешёл под нового победителя
    row = await pool.fetchrow("SELECT winner_kind, winner_id, status FROM dedup_decisions WHERE id = $1", d2)
    assert (row["winner_kind"], row["winner_id"], row["status"]) == ("m", l1, "active")
    new = await pool.fetchrow("SELECT * FROM dedup_decisions WHERE status = 'active' AND loser_id = $1", w)
    assert new["winner_id"] == l1 and new["decided_by"] == "admin"
    # и обратно
    assert (await da.swap_decision(pool, new["id"])).ok
    assert await hidden(pool, "m", w) is False and await hidden(pool, "m", l1) is True


async def test_swap_refused_for_digest_row_and_for_loser_winner(pool):
    digest, l = await post(pool, 1, position=2), await post(pool, 2, ch=222, hidden=True)
    d = await decide(pool, ("m", digest), ("m", l))
    out = await da.swap_decision(pool, d)
    assert not out.ok and "дайджест" in out.text
    info = await da.get_decision(pool, d)
    assert await da.can_swap(pool, info["decision"], info["winner"], info["loser"]) is False
    # победитель сам скрыт другим решением
    top, mid, low = await post(pool, 3), await post(pool, 4, ch=222, hidden=True), await post(pool, 5, ch=333, hidden=True)
    await decide(pool, ("m", top), ("m", mid))
    d2 = await decide(pool, ("m", mid), ("m", low))
    assert not (await da.swap_decision(pool, d2)).ok


async def test_show_record_reverts_ai_decision_and_hide_is_manual(pool):
    w, l = await post(pool, 1), await post(pool, 2, ch=222, hidden=True)
    d = await decide(pool, ("m", w), ("m", l))
    assert (await da.show_record(pool, "m", l)).ok
    assert await hidden(pool, "m", l) is False and await status(pool, d) == "reverted"
    assert (await da.hide_record(pool, "m", w)).ok
    assert await hidden(pool, "m", w) is True
    assert await pool.fetchval("SELECT count(*) FROM dedup_decisions WHERE status = 'active'") == 0   # ручное скрытие без решения
    assert not (await da.show_record(pool, "m", 99999)).ok


async def test_release_ai_hidden(pool):
    w = await post(pool, 1)
    l1, l2 = await post(pool, 2, ch=222, hidden=True), await post(pool, 3, ch=333, hidden=True)
    e = await event(pool, "http://e", hidden=True)
    manual = await post(pool, 4, ch=444, hidden=True)                    # скрыт вручную — решения нет
    adm = await post(pool, 5, ch=555, hidden=True)
    await decide(pool, ("m", w), ("m", l1))
    await decide(pool, ("m", w), ("m", l2))
    await decide(pool, ("m", w), ("e", e))
    await decide(pool, ("m", w), ("m", adm), decided_by="admin")
    dry = await da.release_ai_hidden(pool)
    assert (dry["posts"], dry["events"], dry["applied"]) == (2, 1, False)
    assert await hidden(pool, "m", l1) is True                           # без apply ничего не меняется
    out = await da.release_ai_hidden(pool, apply=True)
    assert out["applied"] and out["decisions"] == 3
    assert [await hidden(pool, "m", x) for x in (l1, l2)] == [False, False] and await hidden(pool, "e", e) is False
    assert await hidden(pool, "m", manual) is True and await hidden(pool, "m", adm) is True
    assert await pool.fetchval("SELECT count(*) FROM dedup_decisions WHERE status = 'stale'") == 3


# ------------------------------------------------------------------------------------- экраны

def ctx(pool, **ud):
    update = MagicMock()
    update.effective_chat.id = 1
    update.effective_user.id = 999
    update.callback_query.answer = AsyncMock()
    context = MagicMock()
    context.user_data = dict(ud)
    context.bot_data = {"pool": pool}
    return update, context


def buttons(call):
    kb = call.kwargs["keyboard"]
    return [b.callback_data for row in kb.inline_keyboard for b in row]


async def test_hidden_event_card_shows_who_hid_and_winner_hint(pool):
    w, l = await post(pool, 1), await event(pool, "http://e", hidden=True)
    await decide(pool, ("m", w), ("e", l), reason="тот же концерт")
    update, context = ctx(pool, admin_hidden_event_ids=[da.make_ref("e", l), da.make_ref("m", w)], admin_hidden_event_index=0)
    edit = AsyncMock()
    with patch.object(admin_panel, "_edit_contact_window", edit):
        await admin_panel._show_admin_hidden_event(update, context, pool)
        text = edit.call_args.kwargs["text"]
        assert "Скрыто AI" in text and "тот же концерт" in text and f"admin_hidden:show:e{l}" in buttons(edit.call_args)
        context.user_data["admin_hidden_event_index"] = 1
        await admin_panel._show_admin_hidden_event(update, context, pool)
        assert "Победитель" in edit.call_args.kwargs["text"] and f"admin_hidden:hide:m{w}" in buttons(edit.call_args)


async def test_hidden_action_show_reverts_decision(pool):
    w, l = await post(pool, 1), await post(pool, 2, ch=222, hidden=True)
    d = await decide(pool, ("m", w), ("m", l))
    update, context = ctx(pool, admin_hidden_event_ids=[da.make_ref("m", l)], admin_hidden_event_index=0)
    update.callback_query.data = f"admin_hidden:show:m{l}"
    with patch.object(admin_panel, "_require_admin", AsyncMock(return_value=True)), \
         patch.object(admin_panel, "_edit_contact_window", AsyncMock()):
        await admin_panel.handle_admin_hidden_action(update, context)
    assert await hidden(pool, "m", l) is False and await status(pool, d) == "reverted"


async def test_manual_hidden_card(pool):
    p = await post(pool, 1, hidden=True)
    update, context = ctx(pool, admin_hidden_event_ids=[da.make_ref("m", p)], admin_hidden_event_index=0)
    edit = AsyncMock()
    with patch.object(admin_panel, "_edit_contact_window", edit):
        await admin_panel._show_admin_hidden_event(update, context, pool)
    assert "Скрыто вручную" in edit.call_args.kwargs["text"]


async def test_duplicates_routes_by_method(pool):
    w, l = await post(pool, 1), await post(pool, 2, ch=222, hidden=True)
    d = await decide(pool, ("m", w), ("m", l))
    for method, expect_actions in (("ai", True), ("shadow", False)):
        if method == "shadow":
            await pool.execute("UPDATE dedup_decisions SET status = 'proposed' WHERE id = $1", d)
            await pool.execute("UPDATE messages SET hidden_by_admin = FALSE")
        update, context = ctx(pool)
        edit = AsyncMock()
        with patch.object(admin_duplicates, "_require_admin", AsyncMock(return_value=True)), \
             patch.dict(admin_duplicates.config, {"deduplication": {"method": method}}), \
             patch.object(admin_dedup_ai, "_edit_contact_window", edit):
            await admin_duplicates.handle_admin_duplicates(update, context)
        cb = buttons(edit.call_args)
        assert (f"dd:rv:{d}" in cb) is expect_actions
        if method == "shadow":
            assert "shadow" in edit.call_args.kwargs["text"]


async def test_dd_action_restore_and_stale_click(pool):
    w, l = await post(pool, 1), await post(pool, 2, ch=222, hidden=True)
    d = await decide(pool, ("m", w), ("m", l))
    update, context = ctx(pool, dd_ids=[d], dd_idx=0, dd_status="active")
    update.callback_query.data = f"dd:rv:{d}"
    edit = AsyncMock()
    with patch.object(admin_dedup_ai, "_require_admin", AsyncMock(return_value=True)), \
         patch.object(admin_dedup_ai, "_edit_contact_window", edit):
        await admin_dedup_ai.handle_dd_action(update, context)
        assert await hidden(pool, "m", l) is False and "Возвращено" in edit.call_args.kwargs["text"]
        await admin_dedup_ai.handle_dd_action(update, context)              # повторное нажатие
        assert "изменилось" in edit.call_args.kwargs["text"]


async def test_non_admin_cannot_act(pool):
    w, l = await post(pool, 1), await post(pool, 2, ch=222, hidden=True)
    d = await decide(pool, ("m", w), ("m", l))
    update, context = ctx(pool, dd_ids=[d], dd_idx=0)
    update.callback_query.data = f"dd:rv:{d}"
    with patch.object(admin_dedup_ai, "_require_admin", AsyncMock(return_value=False)):
        await admin_dedup_ai.handle_dd_action(update, context)
    assert await hidden(pool, "m", l) is True


# ------------------------------------------------------------------------------------- run_once

def args(**kw):
    ns = argparse.Namespace(force=False, day=[], max_days=0, no_alert=True, ignore_collectors=False,
                            release=False, yes=False, include_admin=False)
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


async def two_dates(pool):
    for i, ch in ((1, 111), (2, 222)):
        await pool.execute("INSERT INTO messages (id, position, channel_id, text, date, published_at) VALUES ($1,0,$2,$3,$4,now())",
                           i, ch, "Концерт группы Икс в клубе Y, 20:00 — вход свободный " + str(ch), DAY)


async def test_run_once_force_recomputes_dates(pool):
    await two_dates(pool)
    cfg = {**CFG, "method": "shadow"}
    out = []
    assert await ro.run_once(pool, cfg, args(), client=client(), out=out.append) == 0
    again = await ro.run_once(pool, cfg, args(), client=client(), out=out.append)
    assert again == 0 and "дат 0/0" in out[-1]                              # отпечаток не изменился — пропуск
    await ro.run_once(pool, cfg, args(force=True), client=client(), out=out.append)
    assert "дат 1/1" in out[-1]                                             # --force пересчитал


async def test_run_once_refuses_when_locked_or_collectors_running(pool):
    await two_dates(pool)
    cfg = {**CFG, "method": "shadow"}
    out = []
    async with try_lock(pool) as got:
        assert got
        assert await ro.run_once(pool, cfg, args(), client=client(), out=out.append) == 3
        assert "уже работает" in out[-1]
    await pool.execute("INSERT INTO collector_runs (service) VALUES ('collector')")
    assert await ro.run_once(pool, cfg, args(), client=client(), out=out.append) == 3
    assert await ro.run_once(pool, cfg, args(ignore_collectors=True), client=client(), out=out.append) == 0


async def test_run_once_refuses_bad_method(pool):
    out = []
    assert await ro.run_once(pool, {**CFG, "method": "legacy"}, args(), client=client(), out=out.append) == 2


async def test_run_once_release(pool):
    w, l = await post(pool, 1), await post(pool, 2, ch=222, hidden=True)
    await decide(pool, ("m", w), ("m", l))
    out = []
    assert await ro.run_once(pool, {**CFG, "method": "ai"}, args(release=True, yes=True), out=out.append) == 2
    assert await hidden(pool, "m", l) is True                               # при ai откат запрещён
    cfg = {**CFG, "method": "shadow"}
    assert await ro.run_once(pool, cfg, args(release=True), out=out.append) == 0
    assert "добавьте --yes" in out[-1] and await hidden(pool, "m", l) is True
    assert await ro.run_once(pool, cfg, args(release=True, yes=True), out=out.append) == 0
    assert await hidden(pool, "m", l) is False


async def test_service_tick_skips_when_locked(pool):
    from deduplicator import main as svc
    from deduplicator.schedule import ServiceSettings
    await two_dates(pool)
    cfg = {**CFG, "method": "shadow"}
    async with try_lock(pool) as got:
        assert got
        assert await svc.tick(pool, client(), cfg, ServiceSettings(settle_seconds=0)) is None
    assert await pool.fetchval("SELECT count(*) FROM dedup_runs") == 0


# ------------------------------------------------------------------------------------- чистка истории

async def test_purge_dedup_history(pool):
    w, l = await post(pool, 1), await post(pool, 2, ch=222)
    old, fresh = date.today() - timedelta(days=90), date.today() - timedelta(days=5)
    for day in (old, fresh):
        await pool.execute("INSERT INTO dedup_decisions (day, winner_kind, winner_id, loser_kind, loser_id, status) "
                           "VALUES ($1,'m',$2,'m',$3,'stale')", day, w + int(day == fresh), l + 10 * int(day == fresh))
        await pool.execute("INSERT INTO dedup_dates (day, fingerprint) VALUES ($1, 'x')", day)
    # прогоны: старый ai (последний ai — остаётся), старый shadow (удаляется), свежий shadow (последний вообще)
    await pool.execute("INSERT INTO dedup_runs (started_at, method, status) VALUES (now() - interval '100 days', 'shadow', 'ok')")
    await pool.execute("INSERT INTO dedup_runs (started_at, method, status) VALUES (now() - interval '90 days', 'ai', 'ok')")
    await pool.execute("INSERT INTO dedup_runs (started_at, method, status) VALUES (now() - interval '80 days', 'shadow', 'ok')")
    await pool.execute("INSERT INTO dedup_runs (started_at, method, status) VALUES (now() - interval '1 days', 'shadow', 'ok')")
    # коллекторы: старый завершённый, свежий завершённый, идущий
    await pool.execute("INSERT INTO collector_runs (service, started_at, heartbeat_at, finished_at, status) "
                       "VALUES ('c', now() - interval '40 days', now() - interval '40 days', now() - interval '40 days', 'ok')")
    for _ in range(60):
        await pool.execute("INSERT INTO collector_runs (service, finished_at, status) VALUES ('c', now() - interval '20 days', 'ok')")
    await pool.execute("INSERT INTO collector_runs (service) VALUES ('c')")
    out = await purge_dedup_history(pool, 60)
    assert out["decisions"] == 1 and out["dates"] == 1
    assert out["runs"] == 2                                              # старый shadow 100д и 80д; ai 90д остаётся
    assert await pool.fetchval("SELECT count(*) FROM dedup_runs WHERE method = 'ai'") == 1
    assert out["collector_runs"] == 12                                   # 61 завершённых старше 14д; 49 из них входят в последние 50 строк вместе с идущим
    assert await pool.fetchval("SELECT count(*) FROM collector_runs WHERE finished_at IS NULL") == 1
    again = await purge_dedup_history(pool, 60)
    assert again == {"decisions": 0, "dates": 0, "runs": 0, "collector_runs": 0}
