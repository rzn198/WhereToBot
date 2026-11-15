"""Пуш из избранного с БД: выборка, ссылки, журнал push_log, повторы, блокировки, режимы, сводка, предпросмотр (нужна DEDUP_TEST_DSN)."""
import os
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest
from telegram.error import Forbidden, NetworkError

from bot.notifications import push
from db import push_log
from shared.config import config
from tests.test_fav_transfer_pg import event, post, put

DSN = os.getenv("DEDUP_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="нужна DEDUP_TEST_DSN (Postgres со схемой проекта)")
TODAY = date(2026, 11, 15)
ADMIN = 999
U1, U2, U3 = 101, 102, 103


@pytest.fixture
async def pool(monkeypatch):
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=5)
    for t in ("dedup_decisions", "channels", "messages", "events", "favorites", "event_favorites", "hidden_events",
              "event_hidden", "push_log"):
        await p.execute(f"TRUNCATE {t} RESTART IDENTITY CASCADE")
    monkeypatch.setattr(push, "SEND_DELAY", 0)
    monkeypatch.setattr(push, "RETRY_PAUSE", 0)
    monkeypatch.setitem(config, "push", {**config["push"], "mode": "on"})
    monkeypatch.setitem(config["telegram"], "admin_user_id", ADMIN)
    yield p
    await p.close()


def make_bot(fail=None):
    """fail: {user_id: [исключения по порядку попыток]}"""
    bot = MagicMock()
    attempts: dict = {}

    async def send(chat_id, **kw):
        queue = (fail or {}).get(chat_id)
        if queue:
            n = attempts.get(chat_id, 0)
            attempts[chat_id] = n + 1
            if n < len(queue) and queue[n] is not None:
                raise queue[n]
        return MagicMock()
    bot.send_message = AsyncMock(side_effect=send)
    return bot


def to_user(bot, user_id):
    return [c.kwargs for c in bot.send_message.call_args_list if c.kwargs["chat_id"] == user_id]


async def log_status(pool, day=TODAY):
    return {r["user_id"]: r["status"] for r in await pool.fetch("SELECT user_id, status FROM push_log WHERE push_date=$1", day)}


async def channel(pool, ch, username):
    await pool.execute("INSERT INTO channels (channel_id, username, title) VALUES ($1, $2, 't')", ch, username)


# ------------------------------------------------------------------ выборка и ссылки
async def test_fetch_items_posts_first_links_and_filters(pool):
    await channel(pool, 111, "clubx")
    p_open = await post(pool, 11, ch=111, day=TODAY, text="Открытый канал: концерт, 20:00")
    p_closed = await post(pool, 22, ch=222, day=TODAY, text="Закрытый канал: вечеринка")
    p_hid = await post(pool, 33, ch=111, day=TODAY, text="Скрыт админом", hidden=True)
    p_user_hid = await post(pool, 44, ch=111, day=TODAY, text="Скрыт пользователем")
    p_other_day = await post(pool, 55, ch=111, day=TODAY + timedelta(days=1), text="Завтра")
    e_ok = await event(pool, "https://tickets.example/e1", day=TODAY)
    e_hid = await event(pool, "https://tickets.example/e2", day=TODAY, hidden=True)
    for p_ in (p_open, p_closed, p_hid, p_user_hid, p_other_day):
        await put(pool, U1, "m", p_, "fav")
    await put(pool, U1, "m", p_user_hid, "hid")
    for e_ in (e_ok, e_hid):
        await put(pool, U1, "e", e_, "fav")
    items = (await push.fetch_items(pool, TODAY))[U1]
    assert [i.kind for i in items] == ["m", "m", "e"]
    by_text = {i.text: i.url for i in items}
    assert by_text["Открытый канал: концерт, 20:00"] == "https://t.me/clubx/11"
    assert by_text["Закрытый канал: вечеринка"] == "https://t.me/c/222/22"
    assert by_text["https://tickets.example/e1"] == "https://tickets.example/e1"
    assert len(items) == 3


async def test_fetch_items_single_user_filter(pool):
    p_ = await post(pool, 1, day=TODAY)
    await put(pool, U1, "m", p_, "fav")
    await put(pool, U2, "m", p_, "fav")
    assert set(await push.fetch_items(pool, TODAY)) == {U1, U2}
    assert set(await push.fetch_items(pool, TODAY, U2)) == {U2}


# ------------------------------------------------------------------ отправка и журнал
async def test_send_logs_and_second_run_does_not_resend(pool):
    p_ = await post(pool, 1, day=TODAY)
    await put(pool, U1, "m", p_, "fav")
    await put(pool, U2, "m", p_, "fav")
    bot = make_bot()
    st = await push.send_push_notifications(bot, pool, today=TODAY)
    assert st["sent"] == 2 and await log_status(pool) == {U1: "sent", U2: "sent"}
    assert len(to_user(bot, U1)) == 1
    kb = to_user(bot, U1)[0]["reply_markup"].inline_keyboard
    assert [r[0].callback_data for r in kb] == ["alert:fav", "alert:dismiss"]
    bot2 = make_bot()
    st2 = await push.send_push_notifications(bot2, pool, today=TODAY)
    assert st2["sent"] == 0 and to_user(bot2, U1) == [] and to_user(bot2, U2) == []
    assert bot2.send_message.await_count == 0          # и сводки нет: ничего не произошло


async def test_catch_up_only_sends_to_those_not_sent(pool):
    p_ = await post(pool, 1, day=TODAY)
    await put(pool, U1, "m", p_, "fav")
    await push_log.record(pool, TODAY, U1, "sent", 1)
    await put(pool, U2, "m", p_, "fav")
    bot = make_bot()
    await push.send_push_notifications(bot, pool, today=TODAY)
    assert to_user(bot, U1) == [] and len(to_user(bot, U2)) == 1


async def test_blocked_is_logged_then_skipped_for_week_then_retried(pool):
    posts = {}
    for k in range(0, 9):
        posts[k] = await post(pool, 100 + k, day=TODAY + timedelta(days=k))
        await put(pool, U1, "m", posts[k], "fav")
    bot = make_bot({U1: [Forbidden("blocked")]})
    st = await push.send_push_notifications(bot, pool, today=TODAY)
    assert st["blocked"] == 1 and await log_status(pool) == {U1: "blocked"}
    bot = make_bot()
    st = await push.send_push_notifications(bot, pool, today=TODAY + timedelta(days=1))
    assert st["skipped_blocked"] == 1 and to_user(bot, U1) == []
    st = await push.send_push_notifications(bot, pool, today=TODAY + timedelta(days=6))
    assert st["skipped_blocked"] == 1
    st = await push.send_push_notifications(bot, pool, today=TODAY + timedelta(days=8))
    assert st["sent"] == 1 and len(to_user(bot, U1)) == 1


async def test_transient_failure_is_retried_in_same_run(pool):
    p_ = await post(pool, 1, day=TODAY)
    await put(pool, U1, "m", p_, "fav")
    bot = make_bot({U1: [NetworkError("timeout"), None]})
    st = await push.send_push_notifications(bot, pool, today=TODAY)
    assert st["sent"] == 1 and st["failed"] == 0 and await log_status(pool) == {U1: "sent"}


async def test_persistent_failure_logged_failed_and_retried_next_run(pool):
    p_ = await post(pool, 1, day=TODAY)
    await put(pool, U1, "m", p_, "fav")
    bot = make_bot({U1: [NetworkError("down"), NetworkError("down")]})
    st = await push.send_push_notifications(bot, pool, today=TODAY)
    assert st["failed"] == 1 and await log_status(pool) == {U1: "failed"}
    summary = bot.send_message.call_args_list[-1].kwargs
    assert summary["chat_id"] == ADMIN and "ошибок: 1" in summary["text"] and "NetworkError" in summary["text"]
    bot2 = make_bot()
    st2 = await push.send_push_notifications(bot2, pool, today=TODAY)       # догоняющий запуск
    assert st2["sent"] == 1 and await log_status(pool) == {U1: "sent"}


async def test_admin_summary_counts(pool):
    p_ = await post(pool, 1, day=TODAY)
    for u in (U1, U2, U3):
        await put(pool, u, "m", p_, "fav")
    bot = make_bot({U2: [Forbidden("blocked")]})
    await push.send_push_notifications(bot, pool, today=TODAY)
    summary = to_user(bot, ADMIN)[-1]["text"]
    assert "отправлено 2" in summary and "заблокировали бота: 1" in summary and "15 ноября" in summary


async def test_off_mode_does_nothing(pool, monkeypatch):
    p_ = await post(pool, 1, day=TODAY)
    await put(pool, U1, "m", p_, "fav")
    monkeypatch.setitem(config, "push", {**config["push"], "mode": "off"})
    bot = make_bot()
    assert await push.send_push_notifications(bot, pool, today=TODAY) == {}
    assert bot.send_message.await_count == 0 and await log_status(pool) == {}


async def test_shadow_mode_sends_only_admin_summary_and_writes_no_log(pool, monkeypatch):
    p_ = await post(pool, 1, day=TODAY)
    await put(pool, U1, "m", p_, "fav")
    monkeypatch.setitem(config, "push", {**config["push"], "mode": "shadow"})
    bot = make_bot()
    st = await push.send_push_notifications(bot, pool, today=TODAY)
    assert st["users"] == 1 and to_user(bot, U1) == [] and await log_status(pool) == {}
    assert "shadow" in to_user(bot, ADMIN)[0]["text"]
    monkeypatch.setitem(config, "push", {**config["push"], "mode": "on"})    # затем включили — день ещё не отправлен
    bot2 = make_bot()
    assert (await push.send_push_notifications(bot2, pool, today=TODAY))["sent"] == 1


async def test_no_favorites_today_sends_nothing(pool):
    p_ = await post(pool, 1, day=TODAY + timedelta(days=1))
    await put(pool, U1, "m", p_, "fav")
    bot = make_bot()
    await push.send_push_notifications(bot, pool, today=TODAY)
    assert bot.send_message.await_count == 0


async def test_long_favorites_split_into_parts_buttons_on_last(pool):
    for i in range(40):
        p_ = await post(pool, 200 + i, day=TODAY, text=f"Пост номер {i}. " + "длинный текст афиши " * 40)
        await put(pool, U1, "m", p_, "fav")
    bot = make_bot()
    await push.send_push_notifications(bot, pool, today=TODAY)
    sent = to_user(bot, U1)
    assert len(sent) > 1
    assert all(s["reply_markup"] is None for s in sent[:-1]) and sent[-1]["reply_markup"] is not None
    assert all(len(s["text"]) <= 4096 for s in sent)
    row = await pool.fetchrow("SELECT items, parts FROM push_log WHERE user_id=$1", U1)
    assert row["items"] == 40 and row["parts"] == len(sent)


# ------------------------------------------------------------------ журнал
async def test_purge_old_keeps_recent(pool):
    await push_log.record(pool, date.today() - timedelta(days=40), U1, "sent", 1)
    await push_log.record(pool, date.today() - timedelta(days=3), U1, "sent", 1)
    assert await push_log.purge_old(pool) == 1
    assert await pool.fetchval("SELECT count(*) FROM push_log") == 1


async def test_record_upserts(pool):
    await push_log.record(pool, TODAY, U1, "failed", 2, error="x" * 500)
    await push_log.record(pool, TODAY, U1, "sent", 2)
    row = await pool.fetchrow("SELECT status, error FROM push_log WHERE user_id=$1", U1)
    assert row["status"] == "sent" and row["error"] is None
    await push_log.record(pool, TODAY, U2, "failed", 1, error="x" * 500)
    assert len(await pool.fetchval("SELECT error FROM push_log WHERE user_id=$1", U2)) == 300


# ------------------------------------------------------------------ предпросмотр
async def test_preview_uses_admin_favorites_for_today(pool):
    p_ = await post(pool, 1, day=TODAY, text="Мой пост про концерт")
    await put(pool, ADMIN, "m", p_, "fav")
    await put(pool, U1, "m", p_, "fav")
    bot = make_bot()
    push_today = push.belgrade_today()
    await pool.execute("UPDATE messages SET date = $1", push_today)
    assert await push.send_preview(bot, pool, ADMIN) == "Отправлено ✅"
    sent = to_user(bot, ADMIN)
    assert "Предпросмотр" in sent[0]["text"] and "Мой пост про концерт" in sent[1]["text"]
    assert to_user(bot, U1) == [] and await log_status(pool, push_today) == {}


async def test_preview_nearest_day_then_demo_then_nothing(pool):
    today = push.belgrade_today()
    bot = make_bot()
    assert "нечего" in (await push.send_preview(bot, pool, ADMIN)).lower()          # пусто всё
    await event(pool, "https://tickets.example/demo", day=today)
    assert await push.send_preview(bot, pool, ADMIN) == "Отправлено ✅"             # образец из событий дня
    assert "образец" in to_user(bot, ADMIN)[0]["text"]
    future = await post(pool, 7, day=today + timedelta(days=3), text="Будущий избранный пост")
    await put(pool, ADMIN, "m", future, "fav")
    bot2 = make_bot()
    assert await push.send_preview(bot2, pool, ADMIN) == "Отправлено ✅"
    assert "ближайший день" in to_user(bot2, ADMIN)[0]["text"] and "Будущий избранный пост" in to_user(bot2, ADMIN)[1]["text"]


# ------------------------------------------------------------------ кнопка «⭐ В избранное»: настоящий путь до карточки
def _button_context(pool, menu_id=800):
    bot = MagicMock()
    bot.delete_message = AsyncMock()
    bot.edit_message_text = AsyncMock()
    bot.edit_message_media = AsyncMock()
    bot.send_photo = AsyncMock()
    sent = MagicMock(message_id=1234)
    bot.send_message = AsyncMock(return_value=sent)
    query = MagicMock()
    query.answer = AsyncMock()
    query.message.message_id = 900
    query.data = "alert:fav"
    update = MagicMock(callback_query=query)
    update.effective_chat.id = U1
    update.effective_user.id = U1
    context = MagicMock(bot=bot)
    context.bot_data = {"pool": pool}
    context.user_data = {"menu_message_id": menu_id, "message_type": "text", "user_id": U1}
    return bot, update, context


async def test_favorites_button_opens_favorites_card_as_new_message(pool):
    from bot.handlers import alert_dismiss
    today = date.today()
    p_ = await post(pool, 1, day=today, text="Мой избранный пост про концерт")
    await put(pool, U1, "m", p_, "fav")
    await pool.execute("INSERT INTO user_stats (user_id, menu_message_id, menu_message_type) VALUES ($1, 800, 'text') "
        "ON CONFLICT (user_id) DO UPDATE SET menu_message_id = 800, menu_message_type = 'text'", U1)
    bot, update, context = _button_context(pool)
    await alert_dismiss.handle_alert_open_favorites(update, context)

    deleted = sorted(c.kwargs["message_id"] for c in bot.delete_message.call_args_list)
    assert deleted == [800, 900]                                       # пуш и старое меню удалены
    assert bot.edit_message_text.await_count == 0 and bot.edit_message_media.await_count == 0
    shown = bot.send_message.call_args.kwargs
    assert shown["chat_id"] == U1 and "Мой избранный пост про концерт" in shown["text"]
    assert context.user_data["event_ids"] == [p_] and context.user_data["current_filter"] == "favorites"
    assert context.user_data["menu_message_id"] == 1234                # новое сообщение стало меню
    assert await pool.fetchval("SELECT menu_message_id FROM user_stats WHERE user_id=$1", U1) == 1234
    data = [b.callback_data for r in shown["reply_markup"].inline_keyboard for b in r]
    assert any(d and d.startswith("fav:") for d in data)               # на карточке кнопка ⭐


async def test_favorites_button_lists_events_and_posts_together(pool):
    from bot.handlers import alert_dismiss
    today = date.today()
    p_ = await post(pool, 2, day=today, text="Пост в избранном")
    e_ = await event(pool, "https://tickets.example/x", day=today)
    await put(pool, U1, "m", p_, "fav")
    await put(pool, U1, "e", e_, "fav")
    bot, update, context = _button_context(pool)
    await alert_dismiss.handle_alert_open_favorites(update, context)
    assert context.user_data["event_ids"] == [p_, f"e{e_}"]


async def test_favorites_button_when_nothing_left_says_so(pool):
    from bot.handlers import alert_dismiss
    bot, update, context = _button_context(pool)
    await alert_dismiss.handle_alert_open_favorites(update, context)
    assert "не найдено" in bot.send_message.call_args.kwargs["text"]


async def test_favorites_button_works_when_old_menu_already_gone(pool):
    from bot.handlers import alert_dismiss
    from telegram.error import BadRequest
    p_ = await post(pool, 3, day=date.today(), text="Пост")
    await put(pool, U1, "m", p_, "fav")
    bot, update, context = _button_context(pool)
    bot.delete_message = AsyncMock(side_effect=BadRequest("Message to delete not found"))
    await alert_dismiss.handle_alert_open_favorites(update, context)
    assert bot.send_message.await_count == 1 and context.user_data["menu_message_id"] == 1234


# ------------------------------------------------------------------ ссылка поста: пуш → /start fav_<id> → карточка
def _start_context(pool, args, menu_id=800, msg_id=950):
    bot = MagicMock()
    bot.delete_message = AsyncMock()
    bot.edit_message_text = AsyncMock()
    bot.edit_message_media = AsyncMock()
    bot.send_photo = AsyncMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=1234))
    update = MagicMock()
    update.message.message_id = msg_id
    update.message.text = "/start " + " ".join(args)
    update.effective_chat.id = U1
    update.effective_user.id = U1
    context = MagicMock(bot=bot)
    context.args = args
    context.bot_data = {"pool": pool}
    context.user_data = {"menu_message_id": menu_id, "message_type": "text", "user_id": U1}
    return bot, update, context


def texts(bot):
    return [c.kwargs.get("text") for c in bot.send_message.call_args_list]


async def test_push_sends_deep_link_for_posts_and_it_opens_that_card(pool):
    from bot.handlers.start import handle_start
    today = date.today()
    first = await post(pool, 1, day=today, text="Первый избранный пост")
    second = await post(pool, 2, day=today, text="Второй избранный пост")
    for p_ in (first, second):
        await put(pool, U1, "m", p_, "fav")
    bot = make_bot()
    bot.get_me = AsyncMock(return_value=MagicMock(username="WhereToBotBG"))
    await push.send_push_notifications(bot, pool, today=today)
    msg = to_user(bot, U1)[0]
    urls = [e.url for e in msg["entities"]]
    assert urls == [f"https://t.me/WhereToBotBG?start=fav_{first}", f"https://t.me/WhereToBotBG?start=fav_{second}"]
    # нажатие на вторую ссылку
    payload = urls[1].split("start=")[1]
    bot2, update, context = _start_context(pool, [payload])
    await handle_start(update, context)
    assert context.user_data["event_ids"] == [first, second] and context.user_data["current_index"] == 1
    assert "Второй избранный пост" in bot2.send_message.call_args.kwargs["text"]
    assert sorted(c.kwargs["message_id"] for c in bot2.delete_message.call_args_list) == [800, 950]   # /start и старое меню
    assert bot2.edit_message_text.await_count == 0
    assert context.user_data["menu_message_id"] == 1234


async def test_card_link_opens_winner_when_post_was_hidden_as_duplicate(pool):
    from bot.handlers.start import handle_start
    from tests.test_fav_transfer_pg import decide
    today = date.today()
    loser = await post(pool, 1, day=today, text="Проигравший дубль", hidden=True)
    winner = await post(pool, 2, day=today, text="Победитель дубля")
    await put(pool, U1, "m", winner, "fav")
    await pool.execute("INSERT INTO dedup_decisions (day, winner_kind, winner_id, loser_kind, loser_id, status, decided_by, model, reason) "
                       "VALUES ($1,'m',$2,'m',$3,'active','ai','m1','то же')", today, winner, loser)
    bot, update, context = _start_context(pool, [f"fav_{loser}"])
    await handle_start(update, context)
    assert context.user_data["event_ids"] == [winner] and "Победитель дубля" in bot.send_message.call_args.kwargs["text"]
    assert MISSING not in texts(bot)


MISSING = "Этого события уже нет в избранном"


async def test_card_link_when_star_removed_shows_notice_then_general_list(pool):
    from bot.handlers.start import handle_start
    today = date.today()
    gone = await post(pool, 1, day=today, text="Снятый с избранного")
    kept = await post(pool, 2, day=today, text="Остался в избранном")
    await put(pool, U1, "m", kept, "fav")
    bot, update, context = _start_context(pool, [f"fav_{gone}"])
    await handle_start(update, context)
    sent = texts(bot)
    assert sent[0] == MISSING and "Остался в избранном" in sent[1]
    assert context.user_data["event_ids"] == [kept] and context.user_data["current_index"] == 0


async def test_card_link_with_empty_favorites_and_unknown_id(pool):
    from bot.handlers.start import handle_start
    bot, update, context = _start_context(pool, ["fav_999999"])
    await handle_start(update, context)
    sent = texts(bot)
    assert sent[0] == MISSING and "не найдено" in sent[1]


async def test_card_link_cannot_open_someone_elses_favorite(pool):
    from bot.handlers.start import handle_start
    today = date.today()
    other = await post(pool, 1, day=today, text="Чужое избранное")
    await put(pool, U2, "m", other, "fav")
    bot, update, context = _start_context(pool, [f"fav_{other}"])
    await handle_start(update, context)
    assert all("Чужое избранное" not in (t or "") for t in texts(bot)) and texts(bot)[0] == MISSING


async def test_plain_start_and_bad_payloads_still_show_main_menu(pool):
    from bot.handlers.start import handle_start
    for args in ([], ["fav_x"], ["something"], ["fav_1", "fav_2"]):
        bot, update, context = _start_context(pool, args)
        await handle_start(update, context)
        assert "event_ids" not in context.user_data, args


async def test_post_card_shows_open_in_channel_button(pool):
    from bot.handlers.events import fetch_event_by_id
    from bot.handlers.start import handle_start
    await channel(pool, 111, "clubx")
    today = date.today()
    p_ = await post(pool, 55, ch=111, day=today, text="Пост из открытого канала")
    closed = await post(pool, 66, ch=222, day=today, text="Пост из закрытого канала")
    for x in (p_, closed):
        await put(pool, U1, "m", x, "fav")
    assert (await fetch_event_by_id(pool, p_, U1))["channel_url"] == "https://t.me/clubx/55"
    assert (await fetch_event_by_id(pool, closed, U1))["channel_url"] == "https://t.me/c/222/66"
    bot, update, context = _start_context(pool, [f"fav_{p_}"])
    await handle_start(update, context)
    buttons = [b for r in bot.send_message.call_args.kwargs["reply_markup"].inline_keyboard for b in r]
    assert [b.url for b in buttons if b.text == "📎 Открыть в канале"] == ["https://t.me/clubx/55"]
