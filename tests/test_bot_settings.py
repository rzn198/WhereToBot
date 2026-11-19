"""Настройки бота (bot_settings) и экран «Режим категорий»: приоритет БД над конфигом, откат при ошибках, подтверждение (без БД)."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.handlers import admin_ui_mode as ui
from bot.handlers import categories as cats
from db import settings
from shared.config import config

ADMIN = 777


class FakePool:
    """Минимальная замена asyncpg.Pool для таблицы bot_settings (одна строка по ключу)."""

    def __init__(self, value=None, fail=False):
        self.row = None if value is None else {'value': value, 'updated_at': datetime(2026, 11, 17, 21, 0, tzinfo=timezone.utc),
                                               'updated_by': 1}
        self.fail = fail
        self.reads = 0

    def _check(self):
        if self.fail:
            raise ConnectionError('db down')

    async def fetchval(self, sql, key):
        self._check()
        self.reads += 1
        return self.row['value'] if self.row else None

    async def fetchrow(self, sql, key):
        self._check()
        return self.row

    async def execute(self, sql, *args):
        self._check()
        if sql.lstrip().startswith('INSERT'):
            self.row = {'value': args[1], 'updated_at': datetime.now(timezone.utc), 'updated_by': args[2]}
        else:
            self.row = None


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    settings.clear_cache()
    monkeypatch.setitem(config['telegram'], 'admin_user_id', ADMIN)
    monkeypatch.setitem(config, 'ui', {**(config.get('ui') or {}), 'category_mode': 'cards'})
    yield
    settings.clear_cache()


def make_update(data, user_id=ADMIN, pool=None):
    q = MagicMock(); q.data = data; q.answer = AsyncMock()
    upd = MagicMock(callback_query=q)
    upd.effective_user.id = user_id; upd.effective_chat.id = 1
    ctx = MagicMock(); ctx.user_data = {}; ctx.bot_data = {'pool': pool}
    return upd, ctx, q


# ---------- нормализация и конфиг ----------

@pytest.mark.parametrize('raw,expected', [('cards', 'cards'), (' Window ', 'window'), ('window_admin', 'window_admin'),
                                          ('foo', None), ('', None), (None, None), (True, None)])
def test_normalize_mode(raw, expected):
    assert settings.normalize_mode(raw) == expected


def test_config_default_and_garbage(monkeypatch):
    assert settings.config_category_mode() == 'cards'
    monkeypatch.setitem(config, 'ui', {'category_mode': 'window'})
    assert settings.config_category_mode() == 'window'
    monkeypatch.setitem(config, 'ui', {'category_mode': 'мусор'})
    assert settings.config_category_mode() == 'cards'
    monkeypatch.setitem(config, 'ui', {})
    assert settings.config_category_mode() == 'cards'


def test_config_yaml_category_mode_is_cards_or_absent():
    import yaml
    from shared.config import ROOT
    cfg = yaml.safe_load(open(ROOT / 'config.yaml', encoding='utf-8'))
    assert cfg['ui'].get('category_mode', 'cards') == 'cards'     # ключа может не быть: тогда действует 'cards'


# ---------- приоритет БД над конфигом и откаты ----------

async def test_db_value_overrides_config(monkeypatch):
    monkeypatch.setitem(config, 'ui', {'category_mode': 'cards'})
    assert await settings.get_category_mode(FakePool('window')) == 'window'


async def test_no_db_value_uses_config(monkeypatch):
    monkeypatch.setitem(config, 'ui', {'category_mode': 'window_admin'})
    assert await settings.get_category_mode(FakePool(None)) == 'window_admin'


async def test_db_error_falls_back_to_cards_even_if_config_says_window(monkeypatch):
    monkeypatch.setitem(config, 'ui', {'category_mode': 'window'})
    assert await settings.get_category_mode(FakePool(fail=True)) == 'cards'


async def test_corrupt_db_value_falls_back_to_cards():
    assert await settings.get_category_mode(FakePool('banana')) == 'cards'


async def test_cache_avoids_repeated_reads_and_set_invalidates():
    pool = FakePool('cards')
    await settings.get_category_mode(pool); await settings.get_category_mode(pool)
    assert pool.reads == 1
    await settings.set_setting(pool, settings.KEY_CATEGORY_MODE, 'window', ADMIN)
    assert await settings.get_category_mode(pool) == 'window'


async def test_window_admin_only_for_admin():
    pool = FakePool('window_admin')
    assert await settings.resolve_category_mode(pool, ADMIN) == 'window'
    assert await settings.resolve_category_mode(pool, 5) == 'cards'
    assert await settings.resolve_category_mode(pool, None) == 'cards'


async def test_window_for_everyone_and_cards_for_nobody():
    assert await settings.resolve_category_mode(FakePool('window'), 5) == 'window'
    settings.clear_cache()
    assert await settings.resolve_category_mode(FakePool('cards'), ADMIN) == 'cards'


# ---------- экран админки ----------

async def test_non_admin_is_rejected_and_nothing_written():
    pool = FakePool('cards')
    upd, ctx, q = make_update('uim:set:window_admin', user_id=5, pool=pool)
    await ui.handle_ui_mode(upd, ctx)
    assert q.answer.await_args.kwargs.get('show_alert') is True
    assert pool.row['value'] == 'cards'


async def test_set_window_admin_writes_immediately(monkeypatch):
    pool = FakePool(None)
    sent = AsyncMock(); monkeypatch.setattr(ui, 'edit_or_send', sent)
    upd, ctx, _ = make_update('uim:set:window_admin', pool=pool)
    await ui.handle_ui_mode(upd, ctx)
    assert pool.row['value'] == 'window_admin' and pool.row['updated_by'] == ADMIN
    assert 'Режим изменён: window_admin' in sent.await_args.kwargs['text']


async def test_set_window_needs_confirmation(monkeypatch):
    pool = FakePool('cards')
    sent = AsyncMock(); monkeypatch.setattr(ui, 'edit_or_send', sent)
    upd, ctx, _ = make_update('uim:set:window', pool=pool)
    await ui.handle_ui_mode(upd, ctx)
    assert pool.row['value'] == 'cards'                                       # ещё не записано
    cbs = [b.callback_data for r in sent.await_args.kwargs['keyboard'].inline_keyboard for b in r]
    assert cbs == ['uim:ok:window', 'uim:menu']
    upd, ctx, _ = make_update('uim:ok:window', pool=pool)
    await ui.handle_ui_mode(upd, ctx)
    assert pool.row['value'] == 'window'


async def test_reset_removes_db_value(monkeypatch):
    pool = FakePool('window')
    sent = AsyncMock(); monkeypatch.setattr(ui, 'edit_or_send', sent)
    upd, ctx, _ = make_update('uim:reset', pool=pool)
    await ui.handle_ui_mode(upd, ctx)
    assert pool.row is None
    assert 'config.yaml' in sent.await_args.kwargs['text']


async def test_menu_shows_current_config_default_and_reset_button(monkeypatch):
    sent = AsyncMock(); monkeypatch.setattr(ui, 'edit_or_send', sent)
    upd, ctx, _ = make_update('uim:menu', pool=FakePool('window_admin'))
    await ui.handle_ui_mode(upd, ctx)
    text = sent.await_args.kwargs['text']
    assert 'Сейчас: window_admin (из БД' in text and 'По умолчанию из config.yaml: cards' in text
    cbs = [b.callback_data for r in sent.await_args.kwargs['keyboard'].inline_keyboard for b in r]
    assert 'uim:reset' in cbs and 'uim:set:window' in cbs and 'admin_panel:main' in cbs

    sent.reset_mock()
    upd, ctx, _ = make_update('uim:menu', pool=FakePool(None))
    await ui.handle_ui_mode(upd, ctx)
    cbs = [b.callback_data for r in sent.await_args.kwargs['keyboard'].inline_keyboard for b in r]
    assert 'uim:reset' not in cbs and 'из config.yaml' in sent.await_args.kwargs['text']


async def test_db_error_on_write_alerts_admin_and_does_not_crash(monkeypatch):
    monkeypatch.setattr(ui, 'edit_or_send', AsyncMock())
    upd, ctx, q = make_update('uim:set:cards', pool=FakePool(fail=True))
    await ui.handle_ui_mode(upd, ctx)
    assert q.answer.await_args.kwargs.get('show_alert') is True


async def test_unknown_mode_rejected():
    upd, ctx, q = make_update('uim:set:hack', pool=FakePool(None))
    await ui.handle_ui_mode(upd, ctx)
    assert q.answer.await_args.kwargs.get('show_alert') is True


def test_callback_data_within_telegram_limit():
    for m in settings.CATEGORY_MODES:
        assert len(f'uim:ok:{m}'.encode()) <= 64


# ---------- диспетчер в обработчике категории ----------

async def test_category_press_opens_day_screen_in_cards_and_for_non_admin_in_window_admin(monkeypatch):
    for mode in ('cards', 'window_admin'):                              # пользователь 1 не админ → карточки
        settings.clear_cache()
        rendered = AsyncMock()
        monkeypatch.setattr(cats, '_render_window', rendered)
        monkeypatch.setattr(cats, 'update_user_stats', AsyncMock())
        upd, ctx, q = make_update('cat:concerts', pool=FakePool(mode))
        await cats.handle_category(upd, ctx)
        rendered.assert_awaited_once()


async def test_category_press_goes_to_list_for_admin_in_window_admin(monkeypatch):
    from bot.handlers import category_window as cw
    shown = AsyncMock(return_value=True)
    monkeypatch.setattr(cw, 'try_show_window', shown)
    monkeypatch.setattr(cats, '_render_window', AsyncMock())
    monkeypatch.setattr(cats, 'update_user_stats', AsyncMock())
    upd, ctx, q = make_update('cat:concerts', user_id=ADMIN, pool=FakePool('window_admin'))
    await cats.handle_category(upd, ctx)
    shown.assert_awaited_once()
    cats._render_window.assert_not_awaited()
