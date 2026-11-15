from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

from bot.handlers import unnamed as un
from bot.keyboards.event_nav import format_date_short, get_event_nav_keyboard
from bot.keyboards.main_menu import get_main_menu_inline


def _texts(kb):
    return [[b.text for b in row] for row in kb.inline_keyboard]


def test_date_short_format():
    assert format_date_short(date(2026, 11, 14)) == 'Сб 14.11'
    assert format_date_short(date(2026, 1, 5)) == 'Пн 05.01'


def test_card_expand_and_date_on_one_row_arrows_separate():
    kb = get_event_nav_keyboard(0, 5, 'e1', False, is_preview=True, event_date=date(2026, 11, 14))
    t = _texts(kb)
    assert t[0] == ['📋 Все описание', '📅 Сб 14.11']
    assert t[1] == ['🔙', '1 / 5', '🔜']


def test_card_short_text_only_date_and_counter_always():
    t = _texts(get_event_nav_keyboard(2, 9, 'e1', False, event_date=date(2026, 11, 14)))
    assert t[0] == ['📅 Сб 14.11'] and t[1] == ['🔙', '3 / 9', '🔜']


def test_admin_review_button_states():
    def last_admin(**kw):
        kb = get_event_nav_keyboard(0, 1, 'e1', False, is_admin=True, **kw)
        return [b for row in kb.inline_keyboard for b in row if b.callback_data and b.callback_data.startswith('rv:')][0]
    assert last_admin().text == '🔎 Пересмотр'
    assert last_admin(under_review=True).text == '⏳ На пересмотре'
    b = last_admin(reviewed=True)
    assert b.text == '✅ Пересмотрено' and b.callback_data == 'rv:busy'


def test_admin_menu_has_unnamed_button_only_for_admin():
    def cbs(admin):
        return [b.callback_data for row in get_main_menu_inline(is_admin=admin).inline_keyboard for b in row]
    assert 'un:menu' in cbs(True) and 'un:menu' not in cbs(False)


def test_item_keyboards():
    nd = _texts(un.build_item_keyboard('no_date', 5, 3))
    assert nd[1] == ['📅 Установить дату', '🚫 Отклонить'] and nd[2] == ['🛑 Скрыть'] and nd[3] == ['⬅️ Назад']
    nc = _texts(un.build_item_keyboard('no_category', 'e5', 3))
    assert nc[1] == ['🗂 Установить категорию', '⏭ Пропустить']


def test_callback_data_fits_telegram_limit():
    kb = un.build_category_keyboard('e123456789')
    assert all(len(b.callback_data.encode()) <= 64 for row in kb.inline_keyboard for b in row)


async def test_non_admin_rejected():
    upd = MagicMock(); upd.effective_user.id = 1; upd.callback_query.data = 'un:menu'
    upd.callback_query.answer = AsyncMock()
    with patch.object(un, 'is_admin_user', return_value=False):
        await un.handle_unnamed(upd, MagicMock())
    upd.callback_query.answer.assert_awaited_once()


async def test_stale_ref_is_not_decided():
    upd = MagicMock(); upd.effective_user.id = 1; upd.callback_query.data = 'un:skip:7'
    upd.callback_query.answer = AsyncMock()
    ctx = MagicMock(); ctx.user_data = {'un': {'flow': 'no_category', 'ids': [8], 'i': 0}}
    with patch.object(un, 'is_admin_user', return_value=True), \
         patch.object(un.moderation, 'decide', AsyncMock()) as dec, patch.object(un, '_show_item', AsyncMock()):
        await un.handle_unnamed(upd, ctx)
    dec.assert_not_awaited()


async def test_bad_date_text_keeps_input_state():
    upd = MagicMock(); upd.effective_user.id = 1; upd.message.text = '32.13.2026'; upd.message.reply_text = AsyncMock()
    ctx = MagicMock(); ctx.user_data = {'un_input': {'ref': 7}, 'un': {'flow': 'no_date', 'ids': [7], 'i': 0}}
    with patch.object(un, 'is_admin_user', return_value=True):
        assert await un.handle_unnamed_text(upd, ctx) is True
    assert 'un_input' in ctx.user_data
