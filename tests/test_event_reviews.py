"""Пересмотр событий: разбор даты, категории, кнопки админа, ввод текста (без БД)."""
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.handlers import reviews as rv
from bot.utils.categories import CATEGORIES


def rows(kb):
    return [[(b.text, b.callback_data) for b in r] for r in kb.inline_keyboard]


def test_parse_review_date():
    assert rv.parse_review_date('25.12.2026') == date(2026, 12, 25)
    assert rv.parse_review_date(' 5.1.2027 ') == date(2027, 1, 5)
    for bad in ('31.02.2026', '2026-12-25', '25.12', 'завтра', ''):
        assert rv.parse_review_date(bad) is None


def test_category_label_has_other_only_here():
    assert rv.category_label('other') == 'Другое'
    assert rv.category_label('concerts') == 'Концерты'
    assert rv.category_label('zzz') is None
    assert 'Другое' not in {c.label for c in CATEGORIES}


def test_review_window_buttons():
    r = rows(rv.build_review_keyboard('e5'))
    flat = [b for row in r for b in row]
    assert ('📅 Неправильная дата', 'rv:date:e5') in flat
    assert ('🗂 Неправильная категория', 'rv:cat:e5') in flat
    assert ('✍️ Ввести вручную', 'rv:man:e5') in flat
    assert r[-1] == [('⬅️ Назад', 'rv:back')]


def test_category_keyboard_has_all_categories_other_and_back():
    r = rows(rv.build_category_keyboard('e5'))
    cbs = [c for row in r for _, c in row]
    for c in CATEGORIES:
        assert f'rv:setcat:e5:{c.code}' in cbs
    assert 'rv:setcat:e5:other' in cbs
    assert r[-1] == [('⬅️ Назад', 'rv:open:e5')]
    assert all(len(c.encode()) <= 64 for c in cbs)


def test_input_keyboard_has_back():
    assert rows(rv.build_input_keyboard(12))[0][0][1] == 'rv:open:12'


def test_values_for_event_and_post():
    ev = rv.build_review_values('e7', {'title': 'T', 'description': 'D', 'starts_at': date(2026, 12, 1),
                                       'venue': 'V', 'source': 'ticketsrs', 'source_url': 'u', 'event_types': ['Концерты']})
    assert ev['kind'] == 'event' and ev['event_id'] == 7 and ev['message_id'] is None and ev['source'] == 'ticketsrs'
    post = rv.build_review_values(9, {'text': '\n\nПервая строка\nвторая', 'date': date(2026, 12, 2), 'channel_username': 'chan',
                                      'id': 55, 'channel_id': 1, 'event_types': []})
    assert post['kind'] == 'post' and post['message_id'] == 9 and post['title'] == 'Первая строка'
    assert post['source'] == '@chan' and post['source_url'] == 'https://t.me/chan/55'


def update_for(data=None, text=None, user_id=111):
    upd = MagicMock()
    upd.effective_user.id = user_id; upd.effective_chat.id = 1
    if data is not None:
        q = MagicMock(); q.data = data; q.answer = AsyncMock()
        upd.callback_query = q
    if text is not None:
        upd.message.text = text; upd.message.delete = AsyncMock(); upd.message.reply_text = AsyncMock()
    ctx = MagicMock(); ctx.user_data = {}; ctx.bot_data = {'pool': MagicMock()}
    return upd, ctx


@pytest.fixture(autouse=True)
def admin_id():
    with patch.dict(rv.config['telegram'], {'admin_user_id': 111}):
        yield


async def test_non_admin_cannot_use_review_buttons():
    upd, ctx = update_for(data='rv:open:e5', user_id=222)
    await rv.handle_review(upd, ctx)
    assert upd.callback_query.answer.await_args.kwargs.get('show_alert') is True


async def test_busy_button_is_inert():
    upd, ctx = update_for(data='rv:busy')
    with patch.object(rv, 'save_review', AsyncMock()) as save:
        await rv.handle_review(upd, ctx)
    save.assert_not_awaited()


async def test_open_on_already_reviewed_event_does_not_open_window():
    upd, ctx = update_for(data='rv:open:e5')
    with patch.object(rv, 'review_state', AsyncMock(return_value='open')), \
         patch.object(rv, '_show_review_window', AsyncMock()) as win, patch.object(rv, '_back_to_card', AsyncMock()):
        await rv.handle_review(upd, ctx)
    win.assert_not_awaited()


async def test_choosing_category_saves_label_and_returns_to_card():
    upd, ctx = update_for(data='rv:setcat:e5:other')
    with patch.object(rv, 'review_state', AsyncMock(return_value=None)), \
         patch.object(rv, 'save_review', AsyncMock(return_value='created')) as save, \
         patch.object(rv, '_back_to_card', AsyncMock()) as back:
        await rv.handle_review(upd, ctx)
    args = save.await_args
    assert args.args[1:3] == ('e5', 'wrong_category') and args.kwargs['correct_category'] == 'Другое'
    back.assert_awaited_once()


async def test_date_flow_prompt_then_valid_input_saves():
    upd, ctx = update_for(data='rv:date:e5')
    with patch.object(rv, 'review_state', AsyncMock(return_value=None)), patch.object(rv, 'edit_or_send', AsyncMock()):
        await rv.handle_review(upd, ctx)
    assert ctx.user_data['review_input'] == {'mode': 'date', 'ref': 'e5'}

    bad, _ = update_for(text='вчера')
    assert await rv.handle_review_text(bad, ctx) is True
    bad.message.reply_text.assert_awaited_once()
    assert 'review_input' in ctx.user_data                   # ввод остаётся открытым

    good, _ = update_for(text='25.12.2026')
    with patch.object(rv, 'save_review', AsyncMock(return_value='created')) as save, patch.object(rv, '_back_to_card', AsyncMock()):
        assert await rv.handle_review_text(good, ctx) is True
    assert save.await_args.kwargs['correct_date'] == date(2026, 12, 25)
    assert 'review_input' not in ctx.user_data


async def test_manual_flow_saves_text_and_rejects_too_long():
    upd, ctx = update_for(data='rv:man:7')
    with patch.object(rv, 'review_state', AsyncMock(return_value=None)), patch.object(rv, 'edit_or_send', AsyncMock()):
        await rv.handle_review(upd, ctx)
    long_msg, _ = update_for(text='я' * (rv.MANUAL_MAX_CHARS + 1))
    assert await rv.handle_review_text(long_msg, ctx) is True and 'review_input' in ctx.user_data
    msg, _ = update_for(text='Это не событие, а курс')
    with patch.object(rv, 'save_review', AsyncMock(return_value='created')) as save, patch.object(rv, '_back_to_card', AsyncMock()):
        await rv.handle_review_text(msg, ctx)
    assert save.await_args.args[1:3] == (7, 'manual') and save.await_args.kwargs['reason_text'] == 'Это не событие, а курс'


async def test_text_ignored_without_review_state_or_for_non_admin():
    msg, ctx = update_for(text='привет')
    assert await rv.handle_review_text(msg, ctx) is False
    ctx.user_data['review_input'] = {'mode': 'manual', 'ref': 'e1'}
    other, _ = update_for(text='привет', user_id=222)
    assert await rv.handle_review_text(other, ctx) is False


async def test_back_clears_input_state():
    upd, ctx = update_for(data='rv:back')
    ctx.user_data['review_input'] = {'mode': 'date', 'ref': 'e1'}
    with patch.object(rv, '_back_to_card', AsyncMock()) as back:
        await rv.handle_review(upd, ctx)
    assert 'review_input' not in ctx.user_data and back.await_count == 1


async def test_save_failure_is_reported_and_state_cleared():
    upd, ctx = update_for(data='rv:setcat:e5:concerts')
    with patch.object(rv, 'review_state', AsyncMock(return_value=None)), \
         patch.object(rv, 'save_review', AsyncMock(side_effect=RuntimeError('db'))), patch.object(rv, '_back_to_card', AsyncMock()) as back:
        await rv.handle_review(upd, ctx)
    back.assert_not_awaited()
    assert upd.callback_query.answer.await_args.kwargs.get('show_alert') is True
