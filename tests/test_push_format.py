"""Пуш из избранного без БД: формат, ссылки, резка, разбиение, доставка, кнопки, планировщик, конфиг."""
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter

from bot.handlers import alert_dismiss
from bot.notifications import push
from bot.notifications.push import Item, build_messages, post_url, shorten
from collector.entities.utf16 import utf16_slice
from shared.config import _push_section, config

DAY = date(2026, 11, 15)          # воскресенье


def link_texts(text, ents):
    return [(utf16_slice(text, e.offset, e.offset + e.length), e.url) for e in ents]


# ------------------------------------------------------------------ резка
def test_shorten_short_text_unchanged_and_one_line():
    assert shorten("Концерт\n\nв  клубе", 200) == "Концерт в клубе"


def test_shorten_cuts_on_word_boundary_with_ellipsis():
    text = "Большой концерт группы Икс в клубе Игрек совсем скоро"
    assert shorten(text, 30) == "Большой концерт группы Икс в…"      # слово «клубе» не рвётся
    assert shorten(text, 200) == text


def test_shorten_long_word_without_spaces_cut_hard():
    assert shorten("а" * 300, 200) == "а" * 200 + "…"


def test_shorten_strips_trailing_punctuation_before_ellipsis():
    out = shorten("Фестиваль, концерт, показ, лекция, мастер-класс", 21)
    assert out.endswith("…") and not out[:-2].endswith(",")


# ------------------------------------------------------------------ ссылки
def test_post_url_variants():
    assert post_url("clubx", -1, 55) == "https://t.me/clubx/55"
    assert post_url("@clubx", None, 55) == "https://t.me/clubx/55"
    assert post_url(None, 1234567, 55) == "https://t.me/c/1234567/55"
    assert post_url(None, None, 55) == ""
    assert post_url("clubx", 1, None) == ""


# ------------------------------------------------------------------ сборка
def test_header_has_date_and_event_title_is_link():
    items = [Item("e", "Jazz вечер", "Клуб Икс", "https://tickets.example/jazz")]
    [(text, ents)] = build_messages(items, DAY, DAY)
    assert text.startswith("📅 Сегодня, Вс, 15 ноября. Из вашего избранного:")
    assert "1. Jazz вечер — Клуб Икс" in text
    assert link_texts(text, ents) == [("Jazz вечер", "https://tickets.example/jazz")]


def test_post_has_open_link_and_text_is_cut():
    items = [Item("m", "слово " * 100, "", "https://t.me/clubx/55")]
    [(text, ents)] = build_messages(items, DAY, DAY, max_chars=50)
    assert link_texts(text, ents) == [("Открыть пост", "https://t.me/clubx/55")]
    assert "…" in text and "слово слово" in text


def test_items_without_url_have_no_entities():
    [(text, ents)] = build_messages([Item("m", "Пост", "", ""), Item("e", "Событие", "", "")], DAY, DAY)
    assert ents == [] and "1. Пост" in text and "2. Событие" in text and "Открыть" not in text


def test_entity_offsets_are_utf16_after_emoji():
    items = [Item("e", "🎷 Джаз 🎺", "", "https://x.example/1"), Item("e", "😀😀 Второе", "", "https://x.example/2")]
    [(text, ents)] = build_messages(items, DAY, DAY)
    assert link_texts(text, ents) == [("🎷 Джаз 🎺", "https://x.example/1"), ("😀😀 Второе", "https://x.example/2")]


def test_long_push_is_split_with_continuous_numbering_and_valid_links():
    items = [Item("m", f"пост{i} " + "текст " * 150, "", f"https://t.me/c/1/{i}") for i in range(1, 31)]
    parts = build_messages(items, DAY, DAY, max_chars=300)
    assert len(parts) > 1
    for text, ents in parts:
        assert push.utf16_len(text) <= 4096
        for label, url in link_texts(text, ents):
            assert label == "Открыть пост" and url.startswith("https://t.me/c/1/")
    joined = "\n".join(t for t, _ in parts)
    assert all(f"\n{i}. " in "\n" + joined for i in range(1, 31))
    assert parts[0][0].startswith("📅") and not any(t.startswith("📅") for t, _ in parts[1:])
    assert sum(len(e) for _, e in parts) == 30


def test_keyboard_order_and_callbacks():
    rows = push.keyboard().inline_keyboard
    assert [[(b.text, b.callback_data) for b in r] for r in rows] == [
        [("⭐ В избранное", "alert:fav")], [("Закрыть", "alert:dismiss")]]


# ------------------------------------------------------------------ доставка
def bot_with(side_effect):
    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=side_effect)
    return bot


async def test_deliver_ok_keyboard_only_on_last_part(monkeypatch):
    monkeypatch.setattr(push, "SEND_DELAY", 0)
    bot = bot_with(None)
    status, n, err = await push.deliver(bot, 5, [("a", []), ("b", [])])
    assert (status, n, err) == ("sent", 2, None)
    first, last = bot.send_message.call_args_list
    assert first.kwargs["reply_markup"] is None and last.kwargs["reply_markup"] is not None
    assert first.kwargs["link_preview_options"].is_disabled is True


async def test_deliver_forbidden_is_blocked():
    status, n, err = await push.deliver(bot_with(Forbidden("bot was blocked by the user")), 5, [("a", [])])
    assert status == "blocked" and n == 0 and "Forbidden" in err


async def test_deliver_chat_not_found_is_blocked_other_badrequest_failed():
    assert (await push.deliver(bot_with(BadRequest("Chat not found")), 5, [("a", [])]))[0] == "blocked"
    assert (await push.deliver(bot_with(BadRequest("Entity_text_invalid")), 5, [("a", [])]))[0] == "failed"


async def test_deliver_network_error_failed():
    status, _, err = await push.deliver(bot_with(NetworkError("timeout")), 5, [("a", [])])
    assert status == "failed" and "NetworkError" in err


async def test_deliver_retry_after_then_ok(monkeypatch):
    monkeypatch.setattr(push.asyncio, "sleep", AsyncMock())
    bot = bot_with([RetryAfter(2), None])
    assert (await push.deliver(bot, 5, [("a", [])]))[0] == "sent"
    assert bot.send_message.await_count == 2


# ------------------------------------------------------------------ кнопка «В избранное»
async def test_open_favorites_deletes_push_and_menu_resets_state_and_opens_new_list():
    bot = MagicMock()
    bot.delete_message = AsyncMock()
    query = MagicMock()
    query.answer = AsyncMock()
    query.message.message_id = 900
    update = MagicMock(callback_query=query)
    update.effective_chat.id = 7
    update.effective_user.id = 7
    pool = MagicMock()
    pool.execute = AsyncMock()
    context = MagicMock(bot=bot)
    context.bot_data = {"pool": pool}
    context.user_data = {"menu_message_id": 800, "message_type": "photo"}
    with patch.object(alert_dismiss, "open_filter", new=AsyncMock()) as opened:
        await alert_dismiss.handle_alert_open_favorites(update, context)
    deleted = [c.kwargs["message_id"] for c in bot.delete_message.call_args_list]
    assert deleted == [900, 800]
    assert "menu_message_id" not in context.user_data and "message_type" not in context.user_data
    assert "menu_message_id = NULL" in pool.execute.call_args.args[0]
    opened.assert_awaited_once_with(update, context, "favorites")


async def test_open_favorites_survives_delete_errors():
    from telegram.error import TelegramError
    bot = MagicMock()
    bot.delete_message = AsyncMock(side_effect=TelegramError("gone"))
    query = MagicMock()
    query.answer = AsyncMock()
    query.message.message_id = 900
    update = MagicMock(callback_query=query)
    update.effective_chat.id = update.effective_user.id = 7
    context = MagicMock(bot=bot)
    context.bot_data = {}
    context.user_data = {}
    with patch.object(alert_dismiss, "open_filter", new=AsyncMock()) as opened:
        await alert_dismiss.handle_alert_open_favorites(update, context)
    opened.assert_awaited_once()


# ------------------------------------------------------------------ конфиг и планировщик
def test_push_section_defaults_and_validation(monkeypatch):
    monkeypatch.delenv("PUSH_MODE", raising=False)
    d = _push_section({}, {"push_alert_time": "08:30"})
    assert d["mode"] == "on" and d["time"] == "08:30" and d["catchup_until"] == "20:00"
    assert d["max_chars"] == 200 and d["timezone"] == "Europe/Belgrade" and "{date}" in d["header"]
    bad = _push_section({"mode": "boom", "time": "99:99", "max_chars": 5, "catchup_until": "x"}, {})
    assert bad["mode"] == "on" and bad["time"] == "09:00" and bad["max_chars"] == 50 and bad["catchup_until"] == "20:00"
    assert _push_section({"mode": "SHADOW"}, {})["mode"] == "shadow"
    monkeypatch.setenv("PUSH_MODE", "off")
    assert _push_section({"mode": "on"}, {})["mode"] == "off"


async def test_scheduler_push_job_uses_belgrade_time_and_catchup(monkeypatch):
    from bot import scheduler as sch
    monkeypatch.setitem(config, "push", {**config["push"], "mode": "on", "time": "09:00"})
    s = sch.start_bot_scheduler(MagicMock(), MagicMock())
    try:
        job = s.get_job("push_notifications")
        assert str(job.trigger.timezone) == "Europe/Belgrade"
        fields = {f.name: str(f) for f in job.trigger.fields}
        assert fields["hour"] == "9" and fields["minute"] == "0"
        assert s.get_job("push_catchup") is not None
    finally:
        s.shutdown(wait=False)


async def test_scheduler_no_push_jobs_when_off(monkeypatch):
    from bot import scheduler as sch
    monkeypatch.setitem(config, "push", {**config["push"], "mode": "off"})
    s = sch.start_bot_scheduler(MagicMock(), MagicMock())
    try:
        assert s.get_job("push_notifications") is None and s.get_job("push_catchup") is None
        assert s.get_job("cleanup") is not None
    finally:
        s.shutdown(wait=False)


async def test_catch_up_runs_only_inside_window(monkeypatch):
    send = AsyncMock()
    monkeypatch.setattr(push, "send_push_notifications", send)
    monkeypatch.setitem(config, "push", {**config["push"], "mode": "on", "time": "00:00", "catchup_until": "23:59"})
    await push.catch_up_push(MagicMock(), MagicMock())
    monkeypatch.setitem(config, "push", {**config["push"], "time": "00:00", "catchup_until": "00:00"})
    await push.catch_up_push(MagicMock(), MagicMock())
    monkeypatch.setitem(config, "push", {**config["push"], "mode": "shadow", "time": "00:00", "catchup_until": "23:59"})
    await push.catch_up_push(MagicMock(), MagicMock())
    assert send.await_count == 1


# ------------------------------------------------------------------ ссылка поста ведёт на карточку в боте
def test_post_link_points_to_bot_card_when_bot_name_known():
    items = [Item("m", "Пост про концерт", "", "https://t.me/clubx/55", ref=77),
             Item("e", "Событие", "", "https://tickets.example/e")]
    [(text, ents)] = build_messages(items, DAY, DAY, bot_username="WhereToBotBG")
    assert link_texts(text, ents) == [("Открыть в боте", "https://t.me/WhereToBotBG?start=fav_77"),
                                      ("Событие", "https://tickets.example/e")]     # событие по-прежнему на билеты


def test_post_link_falls_back_to_channel_without_bot_name_or_ref():
    item = Item("m", "Пост", "", "https://t.me/clubx/55", ref=77)
    [(text, ents)] = build_messages([item], DAY, DAY)
    assert link_texts(text, ents) == [("Открыть пост", "https://t.me/clubx/55")]
    [(text, ents)] = build_messages([Item("m", "Пост", "", "https://t.me/clubx/55")], DAY, DAY, bot_username="b")
    assert link_texts(text, ents) == [("Открыть пост", "https://t.me/clubx/55")]


def test_deep_link_payload_fits_telegram_limits():
    from bot.utils.links import FAV_START_RE, bot_card_url
    url = bot_card_url("@WhereToBotBG", 123456789)
    assert url == "https://t.me/WhereToBotBG?start=fav_123456789"
    payload = url.split("start=")[1]
    assert len(payload) <= 64 and FAV_START_RE.match(payload)
    for bad in ("fav_", "fav_x", "fav_1; DROP", "fav_-5", "fav_" + "9" * 30, "other_5", "fav_5 6"):
        assert not FAV_START_RE.match(bad)


async def test_bot_username_prefers_attribute_then_get_me_then_none():
    bot = MagicMock(username="AttrBot")
    assert await push.bot_username(bot) == "AttrBot"
    bot = MagicMock()
    bot.get_me = AsyncMock(return_value=MagicMock(username="MeBot"))
    assert await push.bot_username(bot) == "MeBot"
    bot.get_me = AsyncMock(side_effect=NetworkError("down"))
    assert await push.bot_username(bot) is None


# ------------------------------------------------------------------ кнопка «Открыть в канале» на карточке поста
def test_post_card_keyboard_has_channel_button_only_with_url():
    from bot.keyboards.event_nav import get_event_nav_keyboard
    with_url = get_event_nav_keyboard(0, 3, 5, False, source_url="https://t.me/clubx/55").inline_keyboard
    btn = [b for r in with_url for b in r if b.text == "📎 Открыть в канале"]
    assert len(btn) == 1 and btn[0].url == "https://t.me/clubx/55"
    without = get_event_nav_keyboard(0, 3, 5, False).inline_keyboard
    assert not [b for r in without for b in r if "канале" in b.text]
