"""Перенос избранного (⭐) и скрытий (🙈) при дедупликации: матрица, откат, прогон дедупа, админ, бот, push (нужна DEDUP_TEST_DSN)."""
import csv
import io
import os
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest

from bot.handlers import favorites as fav_handler
from bot.handlers.events import _winner_ref, load_event_ids
from db import dedup_admin as da
from db import fav_moves_export, fav_transfer
from deduplicator import runner, store
from deduplicator.engine import candidates, plan_day
from deduplicator.grouping import Group
from deduplicator.priority import PriorityResolver
from tests.test_dedup_service_pg import CFG, client

DSN = os.getenv("DEDUP_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="нужна DEDUP_TEST_DSN (Postgres со схемой проекта)")
DAY = date.today() + timedelta(days=5)
U1, U2, U3 = 101, 102, 103
EARLY = datetime(2026, 1, 1, tzinfo=timezone.utc)
LATE = datetime(2026, 2, 1, tzinfo=timezone.utc)


@pytest.fixture
async def pool():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=5)
    for t in ("dedup_decisions", "dedup_dates", "dedup_runs", "collector_runs", "channels", "messages", "events"):
        await p.execute(f"TRUNCATE {t} RESTART IDENTITY CASCADE")
    for t in ("favorite_moves", "favorites", "event_favorites", "hidden_events", "event_hidden"):
        await p.execute(f"TRUNCATE {t} RESTART IDENTITY CASCADE")
    yield p
    await p.close()


async def post(pool, i, ch=111, hidden=False, text=None, day=DAY):
    return await pool.fetchval(
        "INSERT INTO messages (id, position, channel_id, text, date, published_at, hidden_by_admin) "
        "VALUES ($1,0,$2,$3,$4, now(), $5) RETURNING surrogate_id",
        i, ch, text or f"Концерт группы Икс в клубе Y, 20:00 — вход свободный {ch} {i}", day, hidden)


async def event(pool, url, hidden=False, day=DAY, source="biletrs"):
    return await pool.fetchval(
        "INSERT INTO events (source, source_url, title, starts_at, city, hidden_by_admin) "
        "VALUES ($1, $2, $2, $3, 'Belgrade', $4) RETURNING id", source, url, day, hidden)


def tabs(kind):
    return ("favorites", "msg_id", "hidden_events") if kind == "m" else ("event_favorites", "event_id", "event_hidden")


async def put(pool, user, kind, rid, state, added=EARLY):
    f, c, h = tabs(kind)
    if state == "fav":
        await pool.execute(f"INSERT INTO {f} (user_id, {c}, added_at) VALUES ($1,$2,$3)", user, rid, added)
    elif state == "hid":
        await pool.execute(f"INSERT INTO {h} (user_id, {c}) VALUES ($1,$2)", user, rid)


async def state(pool, user, kind, rid):
    f, c, h = tabs(kind)
    fav = await pool.fetchval(f"SELECT added_at FROM {f} WHERE user_id=$1 AND {c}=$2", user, rid)
    hid = await pool.fetchval(f"SELECT 1 FROM {h} WHERE user_id=$1 AND {c}=$2", user, rid)
    return ("fav+hid" if fav and hid else "fav" if fav else "hid" if hid else "-")


async def decide(pool, winner, loser, status="active", decided_by="ai"):
    return await pool.fetchval(
        "INSERT INTO dedup_decisions (day, winner_kind, winner_id, loser_kind, loser_id, status, decided_by, model, reason) "
        "VALUES ($1,$2,$3,$4,$5,$6,$7,'m1','то же') RETURNING id", DAY, winner[0], winner[1], loser[0], loser[1], status, decided_by)


async def do_transfer(pool, did, loser, winner):
    async with pool.acquire() as conn:
        async with conn.transaction():
            return await fav_transfer.transfer(conn, decision_id=did, day=DAY, loser=loser, winner=winner)


async def do_rollback(pool, ids):
    async with pool.acquire() as conn:
        async with conn.transaction():
            return await fav_transfer.rollback(conn, ids)


# ------------------------------------------------------------------ матрица ⭐/🙈

MATRIX = [("fav", "-", "fav"), ("fav", "fav", "fav"), ("fav", "hid", "fav"),
          ("hid", "-", "hid"), ("hid", "fav", "fav"), ("hid", "hid", "hid"),
          ("-", "fav", "fav"), ("-", "hid", "hid"), ("-", "-", "-")]
KINDS = [("m", "m"), ("m", "e"), ("e", "m"), ("e", "e")]


async def pair(pool, lk, wk):
    mk = {"m": lambda n: post(pool, n, ch=300 + n), "e": lambda n: event(pool, f"u{n}")}
    return (lk, await mk[lk](1)), (wk, await mk[wk](2))


@pytest.mark.parametrize("lk,wk", KINDS)
@pytest.mark.parametrize("ls,ws,expected", MATRIX)
async def test_matrix_transfer_and_rollback(pool, lk, wk, ls, ws, expected):
    loser, winner = await pair(pool, lk, wk)
    did = await decide(pool, winner, loser)
    await put(pool, U1, *loser, ls)
    await put(pool, U1, *winner, ws, added=LATE)
    n = await do_transfer(pool, did, loser, winner)
    assert n == (0 if ls == "-" else 1)
    assert await state(pool, U1, *winner) == expected
    assert await state(pool, U1, *loser) == "-"                           # у проигравшего ничего не осталось
    await do_rollback(pool, [did])                                          # проигравший воскрес: всё как было
    assert await state(pool, U1, *loser) == ls
    assert await state(pool, U1, *winner) == ws


async def test_fav_both_keeps_one_row_with_earlier_added_at(pool):
    loser, winner = await pair(pool, "m", "m")
    did = await decide(pool, winner, loser)
    await put(pool, U1, *loser, "fav", added=EARLY)
    await put(pool, U1, *winner, "fav", added=LATE)
    await do_transfer(pool, did, loser, winner)
    rows = await pool.fetch("SELECT added_at FROM favorites WHERE msg_id = $1", winner[1])
    assert len(rows) == 1 and rows[0]["added_at"] == EARLY
    await do_rollback(pool, [did])
    assert await pool.fetchval("SELECT added_at FROM favorites WHERE msg_id = $1 AND user_id = $2", winner[1], U1) == LATE


async def test_each_user_is_handled_separately(pool):
    loser, winner = await pair(pool, "m", "e")
    did = await decide(pool, winner, loser)
    await put(pool, U1, *loser, "fav")
    await put(pool, U2, *loser, "hid")
    await put(pool, U3, *winner, "fav")
    assert await do_transfer(pool, did, loser, winner) == 2
    assert [await state(pool, u, *winner) for u in (U1, U2, U3)] == ["fav", "hid", "fav"]
    assert await pool.fetchval("SELECT count(*) FROM favorite_moves WHERE decision_id = $1", did) == 2


async def test_rollback_does_not_resurrect_fav_user_already_removed_from_winner(pool):
    loser, winner = await pair(pool, "m", "m")
    did = await decide(pool, winner, loser)
    await put(pool, U1, *loser, "fav")
    await do_transfer(pool, did, loser, winner)
    await pool.execute("DELETE FROM favorites WHERE user_id = $1 AND msg_id = $2", U1, winner[1])    # пользователь снял ⭐ сам
    await do_rollback(pool, [did])
    assert await state(pool, U1, *loser) == "-"


async def test_rollback_is_idempotent(pool):
    loser, winner = await pair(pool, "m", "m")
    did = await decide(pool, winner, loser)
    await put(pool, U1, *loser, "fav")
    await do_transfer(pool, did, loser, winner)
    assert await do_rollback(pool, [did]) == 1 and await do_rollback(pool, [did]) == 0
    assert await state(pool, U1, *loser) == "fav"


async def test_purge_old_moves_keeps_recent(pool):
    loser, winner = await pair(pool, "m", "m")
    did = await decide(pool, winner, loser)
    await put(pool, U1, *loser, "fav")
    await do_transfer(pool, did, loser, winner)
    await pool.execute("UPDATE favorite_moves SET event_date = $1", date.today() - timedelta(days=30))
    assert await fav_transfer.purge_old(pool) == 0                                # дата события + 1 месяц ещё не прошла
    await pool.execute("UPDATE favorite_moves SET event_date = $1", date.today() - timedelta(days=40))
    assert await fav_transfer.purge_old(pool) == 1


# ------------------------------------------------------------------ прогон дедупа: перенос и воскрешение

RES = PriorityResolver(["ch:111"])


async def apply(pool, groups_keys, resolver=RES, live_override=None):
    recs = await store.load_records(pool, DAY, DAY)
    live = (await store.load_live(pool, DAY, DAY)).get(DAY, [])
    cands = candidates(recs, live)
    by = {r.key: r for r in cands}
    groups = [Group([by[k] for k in ks]) for ks in groups_keys]
    plan = plan_day(DAY, cands, groups, live, resolver, "ai")
    moved = await store.apply_day(pool, plan, run_id=None, fingerprint="x", model="m1")
    return plan, moved


async def test_apply_day_moves_favorite_hides_loser_in_one_transaction(pool):
    w, l = await post(pool, 1, ch=111), await post(pool, 2, ch=222)
    await put(pool, U1, "m", l, "fav")
    await put(pool, U2, "m", l, "hid")
    plan, moved = await apply(pool, [[("m", w), ("m", l)]])
    assert moved == 2 and plan.hide == [("m", l)]
    assert await pool.fetchval("SELECT hidden_by_admin FROM messages WHERE surrogate_id = $1", l) is True
    assert [await state(pool, u, "m", w) for u in (U1, U2)] == ["fav", "hid"]
    assert await state(pool, U1, "m", l) == await state(pool, U2, "m", l) == "-"
    # победитель виден в «Избранное», проигравший — нет
    assert await load_event_ids(pool, U1, "favorites") == [w]


async def test_apply_day_second_pass_is_noop(pool):
    w, l = await post(pool, 1, ch=111), await post(pool, 2, ch=222)
    await put(pool, U1, "m", l, "fav")
    await apply(pool, [[("m", w), ("m", l)]])
    plan, moved = await apply(pool, [[("m", w), ("m", l)]])
    assert moved == 0 and plan.insert == [] and await pool.fetchval("SELECT count(*) FROM favorite_moves") == 1


async def test_winner_disappears_loser_is_resurrected_with_favorite(pool):
    w, l = await post(pool, 1, ch=111), await post(pool, 2, ch=222)
    await put(pool, U1, "m", l, "fav")
    await put(pool, U2, "m", l, "hid")
    await apply(pool, [[("m", w), ("m", l)]])
    await pool.execute("UPDATE messages SET hidden_by_admin = TRUE WHERE surrogate_id = $1", w)      # победителя скрыли вручную
    plan, _ = await apply(pool, [])
    assert plan.unhide == [("m", l)]
    assert await pool.fetchval("SELECT hidden_by_admin FROM messages WHERE surrogate_id = $1", l) is False
    assert await state(pool, U1, "m", l) == "fav" and await state(pool, U2, "m", l) == "hid"
    assert await state(pool, U1, "m", w) == "-" and await state(pool, U2, "m", w) == "-"
    assert await pool.fetchval("SELECT bool_and(reverted) FROM favorite_moves") is True


async def test_winner_changes_favorite_follows_new_winner(pool):
    a, b, c = await post(pool, 1, ch=111), await post(pool, 2, ch=222), await post(pool, 3, ch=333)
    await put(pool, U1, "m", c, "fav")
    await apply(pool, [[("m", a), ("m", c)]])
    assert await state(pool, U1, "m", a) == "fav"
    # a пропадает (скрыта вручную), появляется b как новый победитель для c
    await pool.execute("UPDATE messages SET hidden_by_admin = TRUE WHERE surrogate_id = $1", a)
    await apply(pool, [[("m", b), ("m", c)]], resolver=PriorityResolver(["ch:222"]))
    assert await state(pool, U1, "m", b) == "fav" and await state(pool, U1, "m", c) == "-" and await state(pool, U1, "m", a) == "-"


async def test_event_loser_goes_to_post_winner_across_kinds(pool):
    p_, e_ = await post(pool, 1, ch=111), await event(pool, "e1")
    await put(pool, U1, "e", e_, "fav")
    await apply(pool, [[("m", p_), ("e", e_)]], resolver=PriorityResolver(["ch:111", "event:biletrs"]))
    assert await state(pool, U1, "m", p_) == "fav" and await state(pool, U1, "e", e_) == "-"
    assert await load_event_ids(pool, U1, "favorites") == [p_]


async def test_shadow_method_moves_nothing(pool):
    w, l = await post(pool, 1, ch=111), await post(pool, 2, ch=222)
    await put(pool, U1, "m", l, "fav")
    recs = await store.load_records(pool, DAY, DAY)
    by = {r.key: r for r in recs}
    plan = plan_day(DAY, recs, [Group([by[("m", w)], by[("m", l)]])], [], RES, "shadow")
    assert await store.apply_day(pool, plan, run_id=None, fingerprint="x", model="m1") == 0
    assert await state(pool, U1, "m", l) == "fav"


# ------------------------------------------------------------------ прогон целиком (run_dedup)

async def test_run_dedup_moves_favorites_reports_and_exports_csv(pool):
    for i, ch in ((1, 111), (2, 222)):
        await post(pool, i, ch=ch, text="Концерт группы Икс в клубе Y, 20:00 — вход свободный " + str(ch))
    w, l = 1, 2
    sid = {r["id"]: r["surrogate_id"] for r in await pool.fetch("SELECT id, surrogate_id FROM messages")}
    await put(pool, U1, "m", sid[l], "fav")
    res = await runner.run_dedup(pool, client(), CFG, method="ai")
    assert res.status == "ok" and res.stats["fav_moved"] == 1 and res.stats.get("fav_invariant_fixed", 0) == 0
    assert await state(pool, U1, "m", sid[w]) == "fav"
    data = await fav_moves_export.build(pool)
    rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig"))))
    assert rows[0] == fav_moves_export.HEAD and len(rows) == 2 and rows[1][2] == str(U1) and "избранное" in rows[1][3]
    assert await fav_moves_export.build(pool, run_id=res.run_id) is not None
    assert await fav_moves_export.build(pool, run_id=res.run_id + 99) is None
    from deduplicator.notify import run_report
    assert "перенесено избранного/скрытий 1" in run_report(res, "ai")


async def test_fav_invariant_is_healed_after_run(pool):
    w, l = await post(pool, 1, ch=111), await post(pool, 2, ch=222, hidden=True)
    await decide(pool, ("m", w), ("m", l))
    await put(pool, U1, "m", l, "fav")                                    # осталось избранное у скрытого проигравшего
    bad = await fav_transfer.heal_invariant(pool)
    assert len(bad) == 1 and bad[0]["users"] == 1
    assert await state(pool, U1, "m", w) == "fav" and await state(pool, U1, "m", l) == "-"
    assert await fav_transfer.heal_invariant(pool) == []


# ------------------------------------------------------------------ действия админа

async def test_restore_keeps_favorite_on_living_winner(pool):
    w, l = await post(pool, 1, ch=111), await post(pool, 2, ch=222)
    await put(pool, U1, "m", l, "fav")
    await apply(pool, [[("m", w), ("m", l)]])
    did = await pool.fetchval("SELECT id FROM dedup_decisions WHERE status = 'active'")
    assert (await da.restore_loser(pool, did)).ok
    assert await state(pool, U1, "m", w) == "fav" and await state(pool, U1, "m", l) == "-"
    assert await pool.fetchval("SELECT hidden_by_admin FROM messages WHERE surrogate_id = $1", l) is False


async def test_swap_moves_everything_to_new_winner(pool):
    w, l1, l2 = await post(pool, 1, ch=111), await post(pool, 2, ch=222), await post(pool, 3, ch=333)
    await put(pool, U1, "m", l1, "fav")
    await put(pool, U2, "m", l2, "fav")
    await put(pool, U3, "m", w, "fav")
    await apply(pool, [[("m", w), ("m", l1), ("m", l2)]])
    assert [await state(pool, u, "m", w) for u in (U1, U2, U3)] == ["fav"] * 3
    did = await pool.fetchval("SELECT id FROM dedup_decisions WHERE loser_id = $1 AND status = 'active'", l1)
    assert (await da.swap_decision(pool, did, admin_id=1)).ok
    assert [await state(pool, u, "m", l1) for u in (U1, U2, U3)] == ["fav"] * 3         # новый победитель — l1
    assert [await state(pool, u, "m", w) for u in (U1, U2, U3)] == ["-"] * 3
    assert await pool.fetchval("SELECT hidden_by_admin FROM messages WHERE surrogate_id = $1", w) is True


async def test_release_ai_hidden_returns_favorites(pool):
    w, l = await post(pool, 1, ch=111), await post(pool, 2, ch=222)
    await put(pool, U1, "m", l, "fav")
    await apply(pool, [[("m", w), ("m", l)]])
    out = await da.release_ai_hidden(pool, apply=True)
    assert out["applied"] and await state(pool, U1, "m", l) == "fav" and await state(pool, U1, "m", w) == "-"


# ------------------------------------------------------------------ бот: список, карточка, кнопка ⭐, push

async def test_favorites_list_hides_admin_hidden_event_and_post(pool):
    ok, hid = await event(pool, "ok"), await event(pool, "hid", hidden=True)
    pk, ph = await post(pool, 1), await post(pool, 2, hidden=True)
    for e_ in (ok, hid):
        await put(pool, U1, "e", e_, "fav")
    for m_ in (pk, ph):
        await put(pool, U1, "m", m_, "fav")
    refs = await load_event_ids(pool, U1, "favorites")
    assert set(refs) == {pk, f"e{ok}"}


async def test_old_list_card_is_replaced_by_winner(pool):
    w, l = await post(pool, 1, ch=111), await post(pool, 2, ch=222, hidden=True)
    await decide(pool, ("m", w), ("m", l))
    assert await _winner_ref(pool, l) == w and await _winner_ref(pool, w) == w
    e_w, e_l = await event(pool, "w"), await event(pool, "l", hidden=True)
    await decide(pool, ("e", e_w), ("e", e_l))
    assert await _winner_ref(pool, f"e{e_l}") == f"e{e_w}"
    manual = await post(pool, 3, ch=444, hidden=True)                      # скрыта вручную, решения нет
    assert await _winner_ref(pool, manual) == manual


def _update(data, user=U1):
    upd, ctx = MagicMock(), MagicMock()
    upd.callback_query.data = data
    upd.callback_query.answer = AsyncMock()
    upd.effective_user.id = user
    return upd, ctx


async def test_star_on_stale_loser_card_goes_to_winner(pool):
    w, l = await post(pool, 1, ch=111), await post(pool, 2, ch=222, hidden=True)
    await decide(pool, ("m", w), ("m", l))
    upd, ctx = _update(f"fav:add:{l}")
    ctx.bot_data = {"pool": pool}
    ctx.user_data = {"event_ids": [l, 999], "current_index": 0}
    with patch.object(fav_handler, "show_current_event", AsyncMock()):
        await fav_handler.handle_favorites(upd, ctx)
    assert await state(pool, U1, "m", w) == "fav" and await state(pool, U1, "m", l) == "-"
    assert ctx.user_data["event_ids"] == [w, 999]
    upd, ctx = _update(f"fav:remove:{l}")
    ctx.bot_data, ctx.user_data = {"pool": pool}, {"event_ids": [w]}
    with patch.object(fav_handler, "show_current_event", AsyncMock()):
        await fav_handler.handle_favorites(upd, ctx)
    assert await state(pool, U1, "m", w) == "-"


async def test_star_on_visible_record_works_as_before(pool):
    p_, e_ = await post(pool, 1), await event(pool, "x")
    for data in (f"fav:add:{p_}", f"fav:add:e{e_}"):
        upd, ctx = _update(data)
        ctx.bot_data, ctx.user_data = {"pool": pool}, {"event_ids": []}
        with patch.object(fav_handler, "show_current_event", AsyncMock()):
            await fav_handler.handle_favorites(upd, ctx)
    assert await state(pool, U1, "m", p_) == "fav" and await state(pool, U1, "e", e_) == "fav"


async def test_star_waits_for_dedup_hide_and_then_lands_on_winner(pool):
    """Гонка: ⭐ нажали, пока транзакция дедупа скрывает запись. ⭐ не должно остаться на скрытом проигравшем."""
    import asyncio
    w, l = await post(pool, 1, ch=111), await post(pool, 2, ch=222)
    conn = await pool.acquire()
    tx = conn.transaction()
    await tx.start()
    await conn.execute("UPDATE messages SET hidden_by_admin = TRUE WHERE surrogate_id = $1", l)
    await conn.execute("INSERT INTO dedup_decisions (day, winner_kind, winner_id, loser_kind, loser_id, status, decided_by) "
                       "VALUES ($1,'m',$2,'m',$3,'active','ai')", DAY, w, l)
    upd, ctx = _update(f"fav:add:{l}")
    ctx.bot_data, ctx.user_data = {"pool": pool}, {"event_ids": [l]}
    try:
        with patch.object(fav_handler, "show_current_event", AsyncMock()):
            task = asyncio.ensure_future(fav_handler.handle_favorites(upd, ctx))
            await asyncio.sleep(0.3)
            waiting = not task.done()                                    # ждёт FOR SHARE, пока дедуп не закоммитит
            await tx.commit()
            await asyncio.wait_for(task, 5)
    finally:
        if conn.is_in_transaction():
            await tx.rollback()
        await pool.release(conn)
    assert waiting
    assert await state(pool, U1, "m", w) == "fav" and await state(pool, U1, "m", l) == "-"


async def test_push_skips_admin_hidden_records(pool, monkeypatch):
    from bot.notifications import push
    from shared.config import config
    monkeypatch.setitem(config, "push", {**config["push"], "mode": "on"})      # в боевом config.yaml может стоять shadow
    today = date.today()
    ok_p, hid_p = await post(pool, 1, day=today, text="Живой пост про концерт"), await post(pool, 2, day=today, hidden=True, text="Скрытый пост")
    ok_e, hid_e = await event(pool, "a", day=today), await event(pool, "b", day=today, hidden=True)
    for m_ in (ok_p, hid_p):
        await put(pool, U1, "m", m_, "fav")
    for e_ in (ok_e, hid_e):
        await put(pool, U1, "e", e_, "fav")
    bot = MagicMock()
    bot.send_message = AsyncMock()
    with patch.object(push, "SEND_DELAY", 0):
        await push.send_push_notifications(bot, pool, today=today)
    text = bot.send_message.call_args_list[0].kwargs["text"]
    assert "Живой пост" in text and "Скрытый пост" not in text and "\n3." not in text


async def test_run_dedup_light_line_loses_to_post_of_same_channel_and_favorite_moves(pool):
    """Вариант C целиком: строка Zarko (position > 0) против обычного поста того же канала, ⭐ строки уезжает на пост."""
    post_id = await post(pool, 1, ch=111, text="Avoidfest. Cormac. Клуб Y, 21:00. Подробный анонс события и билеты по ссылке")
    line_id = await pool.fetchval(
        "INSERT INTO messages (id, position, channel_id, text, date, published_at) VALUES (1, 3, 111, $1, $2, now()) "
        "RETURNING surrogate_id", "Суббота • Avoidfest, Cormac", DAY)
    await put(pool, U1, "m", line_id, "fav")
    res = await runner.run_dedup(pool, client(), CFG, method="ai")        # CFG без same_source: действуют значения по умолчанию
    assert res.status == "ok"
    assert await pool.fetchval("SELECT hidden_by_admin FROM messages WHERE surrogate_id = $1", line_id) is True
    assert await pool.fetchval("SELECT hidden_by_admin FROM messages WHERE surrogate_id = $1", post_id) is False
    assert await state(pool, U1, "m", post_id) == "fav" and await state(pool, U1, "m", line_id) == "-"
