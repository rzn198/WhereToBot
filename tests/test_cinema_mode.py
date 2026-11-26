"""Режим киноафиши: настройка, приоритеты, экран в админке (без БД)."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.handlers import admin_cinema_mode as cim
from db import settings
from shared.config import config
from tests.test_bot_settings import ADMIN, FakePool


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    settings.clear_cache()
    monkeypatch.setitem(config['telegram'], 'admin_user_id', ADMIN)
    monkeypatch.setitem(config, 'ui', {**(config.get('ui') or {}), 'cinema_mode': 'old'})
    yield
    settings.clear_cache()


def make_update(data, user_id=ADMIN, pool=None):
    q = MagicMock(); q.data = data; q.answer = AsyncMock()
    upd = MagicMock(callback_query=q)
    upd.effective_user.id = user_id; upd.effective_chat.id = 1
    ctx = MagicMock(); ctx.user_data = {}; ctx.bot_data = {'pool': pool}
    return upd, ctx, q


@pytest.mark.parametrize('raw,expected', [('old', 'old'), (' NEW ', 'new'), ('new_admin', 'new_admin'), ('x', None), (None, None), ('', None)])
def test_normalize(raw, expected):
    assert settings.normalize_cinema_mode(raw) == expected


async def test_default_is_old_without_db_value():
    assert await settings.get_cinema_mode(FakePool()) == 'old'


async def test_config_value_used_when_db_empty(monkeypatch):
    monkeypatch.setitem(config, 'ui', {'cinema_mode': 'new'})
    assert await settings.get_cinema_mode(FakePool()) == 'new'


async def test_db_beats_config(monkeypatch):
    monkeypatch.setitem(config, 'ui', {'cinema_mode': 'new'})
    assert await settings.get_cinema_mode(FakePool('old')) == 'old'


async def test_broken_value_and_db_failure_fall_back_to_old():
    assert await settings.get_cinema_mode(FakePool('garbage')) == 'old'
    assert await settings.get_cinema_mode(FakePool('new', fail=True)) == 'old'


@pytest.mark.parametrize('value,user,expected', [
    ('old', ADMIN, 'old'), ('new', 5, 'new'), ('new_admin', ADMIN, 'new'), ('new_admin', 5, 'old'), ('new_admin', None, 'old')])
async def test_resolve_by_user(value, user, expected):
    assert await settings.resolve_cinema_mode(FakePool(value), user) == expected


async def test_menu_shows_current(monkeypatch):
    shown = AsyncMock(); monkeypatch.setattr(cim, 'edit_or_send', shown)
    upd, ctx, q = make_update('cim:menu', pool=FakePool('new_admin'))
    await cim.handle_cinema_mode(upd, ctx)
    kw = shown.call_args.kwargs
    assert 'Сейчас: new_admin' in kw['text']
    labels = [b.text for r in kw['keyboard'].inline_keyboard for b in r]
    assert any(l.startswith('✅ 🧪') for l in labels)


async def test_non_admin_denied(monkeypatch):
    shown = AsyncMock(); monkeypatch.setattr(cim, 'edit_or_send', shown)
    upd, ctx, q = make_update('cim:set:new', user_id=5, pool=FakePool())
    await cim.handle_cinema_mode(upd, ctx)
    q.answer.assert_awaited_with('Нет доступа', show_alert=True)
    shown.assert_not_awaited()


async def test_new_requires_confirmation_then_applies(monkeypatch):
    shown = AsyncMock(); monkeypatch.setattr(cim, 'edit_or_send', shown)
    pool = FakePool()
    upd, ctx, q = make_update('cim:set:new', pool=pool)
    await cim.handle_cinema_mode(upd, ctx)
    assert pool.row is None and 'ВСЕХ' in shown.call_args.kwargs['text']
    upd, ctx, q = make_update('cim:ok:new', pool=pool)
    await cim.handle_cinema_mode(upd, ctx)
    assert pool.row['value'] == 'new'


async def test_new_admin_applies_without_confirmation_and_reset(monkeypatch):
    monkeypatch.setattr(cim, 'edit_or_send', AsyncMock())
    pool = FakePool()
    await cim.handle_cinema_mode(*make_update('cim:set:new_admin', pool=pool)[:2])
    assert pool.row['value'] == 'new_admin'
    await cim.handle_cinema_mode(*make_update('cim:reset', pool=pool)[:2])
    assert pool.row is None


async def test_bad_callback(monkeypatch):
    monkeypatch.setattr(cim, 'edit_or_send', AsyncMock())
    upd, ctx, q = make_update('cim:set:wat', pool=FakePool())
    await cim.handle_cinema_mode(upd, ctx)
    q.answer.assert_awaited_with('Неверный запрос', show_alert=True)
