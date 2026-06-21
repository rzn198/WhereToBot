import json
from datetime import date, datetime, timezone
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from bot.handlers.admin_panel import (
    handle_admin_new_event_save,
    handle_admin_alert_confirm,
)
from db.tables import Tables


def make_update_and_context(user_data: dict | None = None):
    update = MagicMock()
    update.callback_query = MagicMock()
    update.callback_query.answer = AsyncMock()
    update.effective_chat.id = 100
    update.effective_user.id = 999  # admin_id

    context = MagicMock()
    context.user_data = user_data or {}
    context.bot_data = {'pool': AsyncMock()}
    context.bot = MagicMock()
    context.bot.edit_message_text = AsyncMock()
    context.bot.send_message = AsyncMock()

    return update, context


@pytest.mark.asyncio
async def test_new_event_save_uses_unique_negative_id_not_zero():
    update, context = make_update_and_context(user_data={
        'new_event_text': 'Тестовое событие',
        'new_event_date': date(2026, 7, 1),
    })

    with patch('bot.handlers.admin_panel._require_admin', new=AsyncMock(return_value=True)), \
         patch('bot.handlers.admin_panel._edit_contact_window', new=AsyncMock()):
        await handle_admin_new_event_save(update, context)

    pool = context.bot_data['pool']
    pool.execute.assert_called_once()

    call_args = pool.execute.call_args
    sql = call_args.args[0]
    params = call_args.args[1:]

    assert 'ON CONFLICT' in sql
    inserted_id = params[0]
    assert inserted_id != 0, "id не должен быть фиксированным нулём"
    assert inserted_id < 0, "manual_id должен быть отрицательным (не пересекается с Telegram message ID)"


@pytest.mark.asyncio
async def test_new_event_save_two_consecutive_calls_get_different_ids():
    update1, context1 = make_update_and_context(user_data={
        'new_event_text': 'Первое событие',
        'new_event_date': date(2026, 7, 1),
    })
    update2, context2 = make_update_and_context(user_data={
        'new_event_text': 'Второе событие',
        'new_event_date': date(2026, 7, 2),
    })

    with patch('bot.handlers.admin_panel._require_admin', new=AsyncMock(return_value=True)), \
         patch('bot.handlers.admin_panel._edit_contact_window', new=AsyncMock()):
        await handle_admin_new_event_save(update1, context1)
        await handle_admin_new_event_save(update2, context2)

    id1 = context1.bot_data['pool'].execute.call_args.args[1]
    id2 = context2.bot_data['pool'].execute.call_args.args[1]

    assert id1 != id2, "Повторные вызовы должны давать разные id, иначе второй INSERT конфликтует"


@pytest.mark.asyncio
async def test_new_event_save_clears_user_data_after_save():
    update, context = make_update_and_context(user_data={
        'new_event_mode': 'confirm',
        'new_event_text': 'Текст',
        'new_event_date': date(2026, 7, 1),
    })

    with patch('bot.handlers.admin_panel._require_admin', new=AsyncMock(return_value=True)), \
         patch('bot.handlers.admin_panel._edit_contact_window', new=AsyncMock()):
        await handle_admin_new_event_save(update, context)

    assert 'new_event_mode' not in context.user_data
    assert 'new_event_text' not in context.user_data
    assert 'new_event_date' not in context.user_data

@pytest.mark.asyncio
async def test_alert_confirm_inserts_when_no_duplicate_exists():
    update, context = make_update_and_context(user_data={
        'alert_text': 'Важное объявление',
        'alert_scheduled_at': datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc),
        'alert_source': 'new',
    })

    pool = context.bot_data['pool']
    pool.fetchval = AsyncMock(return_value=None)

    with patch('bot.handlers.admin_panel._require_admin', new=AsyncMock(return_value=True)), \
         patch('bot.handlers.admin_panel._edit_contact_window', new=AsyncMock()):
        await handle_admin_alert_confirm(update, context)

    pool.execute.assert_called_once()
    insert_sql = pool.execute.call_args.args[0]
    assert 'INSERT INTO' in insert_sql
    assert Tables.SCHEDULED_ALERTS in insert_sql


@pytest.mark.asyncio
async def test_alert_confirm_skips_insert_when_duplicate_detected():
    update, context = make_update_and_context(user_data={
        'alert_text': 'Важное объявление',
        'alert_scheduled_at': datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc),
        'alert_source': 'new',
    })

    pool = context.bot_data['pool']
    pool.fetchval = AsyncMock(return_value=42)

    with patch('bot.handlers.admin_panel._require_admin', new=AsyncMock(return_value=True)), \
         patch('bot.handlers.admin_panel._edit_contact_window', new=AsyncMock()):
        await handle_admin_alert_confirm(update, context)

    pool.execute.assert_not_called()


@pytest.mark.asyncio
async def test_alert_confirm_rejects_empty_text():
    update, context = make_update_and_context(user_data={
        'alert_text': '   ',
        'alert_scheduled_at': datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc),
        'alert_source': 'new',
    })

    pool = context.bot_data['pool']

    with patch('bot.handlers.admin_panel._require_admin', new=AsyncMock(return_value=True)), \
         patch('bot.handlers.admin_panel._edit_contact_window', new=AsyncMock()):
        await handle_admin_alert_confirm(update, context)

    pool.fetchval.assert_not_called()
    pool.execute.assert_not_called()


@pytest.mark.asyncio
async def test_alert_confirm_clears_user_data_in_both_paths():
    update, context = make_update_and_context(user_data={
        'alert_mode': 'confirm',
        'alert_text': 'Текст',
        'alert_source': 'new',
        'alert_msg_sid': None,
        'alert_scheduled_at': datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc),
        'alert_event_ids': [1, 2, 3],
        'alert_event_index': 0,
    })

    pool = context.bot_data['pool']
    pool.fetchval = AsyncMock(return_value=42)  # дубликат — ветка "пропускаем INSERT"

    with patch('bot.handlers.admin_panel._require_admin', new=AsyncMock(return_value=True)), \
         patch('bot.handlers.admin_panel._edit_contact_window', new=AsyncMock()):
        await handle_admin_alert_confirm(update, context)

    for key in ['alert_mode', 'alert_text', 'alert_source', 'alert_msg_sid',
                'alert_scheduled_at', 'alert_event_ids', 'alert_event_index']:
        assert key not in context.user_data
