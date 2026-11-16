import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from bot.utils.messenger import edit_or_send


def make_context(message_type='text', menu_message_id=123, user_id=None):
    context = MagicMock()
    context.user_data = {
        'message_type': message_type,
        'menu_message_id': menu_message_id,
    }
    if user_id:
        context.user_data['user_id'] = user_id
    context.bot = MagicMock()
    context.bot.edit_message_media = AsyncMock()
    context.bot.edit_message_text = AsyncMock()
    context.bot.delete_message = AsyncMock()
    context.bot.send_photo = AsyncMock(return_value=MagicMock(message_id=456))
    context.bot.send_message = AsyncMock(return_value=MagicMock(message_id=456))
    return context

@pytest.mark.asyncio
async def test_photo_to_photo_uses_edit_message_media():
    context = make_context(message_type='photo')
    await edit_or_send(
        context=context, chat_id=100, text='Текст',
        keyboard=MagicMock(), photo='PHOTO_ID',
    )
    context.bot.edit_message_media.assert_called_once()
    context.bot.edit_message_text.assert_not_called()
    assert context.user_data['message_type'] == 'photo'


@pytest.mark.asyncio
async def test_text_to_photo_uses_edit_message_media():
    context = make_context(message_type='text')
    await edit_or_send(
        context=context, chat_id=100, text='Текст',
        keyboard=MagicMock(), photo='PHOTO_ID',
    )
    context.bot.edit_message_media.assert_called_once()
    assert context.user_data['message_type'] == 'photo'


@pytest.mark.asyncio
async def test_photo_to_text_deletes_and_resends():
    context = make_context(message_type='photo')
    await edit_or_send(
        context=context, chat_id=100, text='Текст',
        keyboard=MagicMock(), photo=None,
    )
    context.bot.delete_message.assert_called_once()
    context.bot.send_message.assert_called_once()
    context.bot.edit_message_media.assert_not_called()
    assert context.user_data['message_type'] == 'text'


@pytest.mark.asyncio
async def test_text_to_text_uses_edit_message_text():
    context = make_context(message_type='text')
    await edit_or_send(
        context=context, chat_id=100, text='Текст',
        keyboard=MagicMock(), photo=None,
    )
    context.bot.edit_message_text.assert_called_once()
    context.bot.edit_message_media.assert_not_called()
    assert context.user_data['message_type'] == 'text'

@pytest.mark.asyncio
async def test_no_existing_message_sends_photo():
    context = make_context(menu_message_id=None)
    await edit_or_send(
        context=context, chat_id=100, text='Текст',
        keyboard=MagicMock(), photo='PHOTO_ID',
    )
    context.bot.send_photo.assert_called_once()
    assert context.user_data['menu_message_id'] == 456


@pytest.mark.asyncio
async def test_no_existing_message_sends_text():
    context = make_context(menu_message_id=None)
    await edit_or_send(
        context=context, chat_id=100, text='Текст',
        keyboard=MagicMock(), photo=None,
    )
    context.bot.send_message.assert_called_once()
    assert context.user_data['menu_message_id'] == 456

@pytest.mark.asyncio
async def test_entities_disable_parse_mode():
    context = make_context(message_type='text')
    entities = [MagicMock()]
    await edit_or_send(
        context=context, chat_id=100, text='Текст',
        keyboard=MagicMock(), photo=None, entities=entities,
    )
    call_kwargs = context.bot.edit_message_text.call_args.kwargs
    assert call_kwargs['parse_mode'] is None
    assert call_kwargs['entities'] == entities

@pytest.mark.asyncio
async def test_long_text_forces_text_type_even_with_photo():
    context = make_context(message_type='text')
    long_text = 'a' * 2000
    await edit_or_send(
        context=context, chat_id=100, text=long_text,
        keyboard=MagicMock(), photo='PHOTO_ID',
    )
    context.bot.edit_message_text.assert_called_once()
    context.bot.edit_message_media.assert_not_called()

@pytest.mark.asyncio
async def test_save_session_called_with_explicit_user_id():
    context = make_context(message_type='text')

    with patch('bot.middleware.session.save_session', new=AsyncMock()) as mock_save:
        await edit_or_send(
            context=context, chat_id=100, text='Текст',
            keyboard=MagicMock(), photo=None, user_id=999,
        )
        mock_save.assert_called_once_with(context, 999)


@pytest.mark.asyncio
async def test_save_session_called_with_user_id_from_context():
    context = make_context(message_type='text', user_id=777)

    with patch('bot.middleware.session.save_session', new=AsyncMock()) as mock_save:
        await edit_or_send(
            context=context, chat_id=100, text='Текст',
            keyboard=MagicMock(), photo=None,
        )
        mock_save.assert_called_once_with(context, 777)


@pytest.mark.asyncio
async def test_save_session_not_called_without_user_id():
    context = make_context(message_type='text')

    with patch('bot.middleware.session.save_session', new=AsyncMock()) as mock_save:
        await edit_or_send(
            context=context, chat_id=100, text='Текст',
            keyboard=MagicMock(), photo=None,
        )
        mock_save.assert_not_called()


@pytest.mark.asyncio
async def test_save_session_called_on_photo_to_photo_transition():
    context = make_context(message_type='photo', user_id=555)

    with patch('bot.middleware.session.save_session', new=AsyncMock()) as mock_save:
        await edit_or_send(
            context=context, chat_id=100, text='Текст',
            keyboard=MagicMock(), photo='PHOTO_ID',
        )
        mock_save.assert_called_once_with(context, 555)
