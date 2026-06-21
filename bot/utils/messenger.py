from loguru import logger
from telegram import InlineKeyboardMarkup, InputMediaPhoto, LinkPreviewOptions
from telegram.error import BadRequest, TelegramError

CAPTION_LIMIT = 1024


async def edit_or_send(
    context,
    chat_id: int,
    text: str,
    keyboard: InlineKeyboardMarkup,
    photo: str | None = None,
    parse_mode: str = 'Markdown',
    entities=None,
    disable_link_preview: bool = False,
    user_id: int | None = None,
) -> None:
    effective_user_id = user_id or context.user_data.get('user_id')

    use_photo = bool(photo and len(text) <= CAPTION_LIMIT)
    target_type = 'photo' if use_photo else 'text'

    current_type = context.user_data.get('message_type', 'text')
    menu_message_id = context.user_data.get('menu_message_id')

    link_preview = LinkPreviewOptions(is_disabled=True) if disable_link_preview else None

    effective_parse_mode = parse_mode if not entities else None

    if menu_message_id:
        try:
            if current_type == 'photo' and target_type == 'photo':
                logger.debug(f"MESSENGER | photo→photo | msg_id={menu_message_id}")
                await context.bot.edit_message_media(
                    chat_id=chat_id,
                    message_id=menu_message_id,
                    media=InputMediaPhoto(
                        media=photo,
                        caption=text,
                        parse_mode=effective_parse_mode,
                        caption_entities=entities,
                    ),
                    reply_markup=keyboard,
                )
                context.user_data['message_type'] = 'photo'
                await _save_session(context, effective_user_id)
                return

            elif current_type == 'text' and target_type == 'photo':
                logger.debug(f"MESSENGER | text→photo | msg_id={menu_message_id}")
                await context.bot.edit_message_media(
                    chat_id=chat_id,
                    message_id=menu_message_id,
                    media=InputMediaPhoto(
                        media=photo,
                        caption=text,
                        parse_mode=effective_parse_mode,
                        caption_entities=entities,
                    ),
                    reply_markup=keyboard,
                )
                context.user_data['message_type'] = 'photo'
                await _save_session(context, effective_user_id)
                return

            elif current_type == 'photo' and target_type == 'text':
                logger.debug(f"MESSENGER | photo→text | msg_id={menu_message_id}")
                try:
                    await context.bot.delete_message(
                        chat_id=chat_id,
                        message_id=menu_message_id,
                    )
                    logger.debug(f"MESSENGER_DELETED | msg_id={menu_message_id}")
                except Exception as del_e:
                    logger.warning(f"MESSENGER_DELETE_FAIL | {del_e}")
                context.user_data.pop('menu_message_id', None)
                menu_message_id = None

            else:
                logger.debug(f"MESSENGER | text→text | msg_id={menu_message_id}")
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=menu_message_id,
                    text=text,
                    entities=entities,
                    reply_markup=keyboard,
                    parse_mode=effective_parse_mode,
                    link_preview_options=link_preview,
                )
                context.user_data['message_type'] = 'text'
                await _save_session(context, effective_user_id)
                return

        except BadRequest as e:
            logger.warning(f"MESSENGER_FAIL | {e} | msg_id={menu_message_id}")
            try:
                await context.bot.delete_message(
                    chat_id=chat_id,
                    message_id=menu_message_id,
                )
                logger.debug(f"MESSENGER_DELETED | msg_id={menu_message_id}")
            except Exception as del_e:
                logger.warning(f"MESSENGER_DELETE_FAIL | {del_e}")
            context.user_data.pop('menu_message_id', None)
            menu_message_id = None

        except TelegramError as e:
            logger.error(f"MESSENGER_ERROR | {e} | msg_id={menu_message_id}")
            context.user_data.pop('menu_message_id', None)
            menu_message_id = None

    if use_photo:
        logger.debug(f"MESSENGER_SEND | type=photo | chat_id={chat_id}")
        try:
            sent = await context.bot.send_photo(
                chat_id=chat_id,
                photo=photo,
                caption=text,
                caption_entities=entities if entities else None,
                reply_markup=keyboard,
                parse_mode=effective_parse_mode,
            )
            context.user_data['message_type'] = 'photo'
        except BadRequest as e:
            logger.warning(f"MESSENGER_PHOTO_INVALID | {e} | photo={photo!r}")
            sent = await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                entities=entities,
                reply_markup=keyboard,
                parse_mode=effective_parse_mode,
                link_preview_options=link_preview,
            )
            context.user_data['message_type'] = 'text'
    else:
        logger.debug(f"MESSENGER_SEND | type=text | chat_id={chat_id}")
        sent = await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            entities=entities,
            reply_markup=keyboard,
            parse_mode=effective_parse_mode,
            link_preview_options=link_preview,
        )
        context.user_data['message_type'] = 'text'

    context.user_data['menu_message_id'] = sent.message_id
    logger.debug(f"MESSENGER_SENT | type={context.user_data['message_type']} | new_msg_id={sent.message_id}")
    await _save_session(context, effective_user_id)


async def _save_session(context, user_id: int | None) -> None:
    if not user_id:
        return
    from bot.middleware.session import save_session
    await save_session(context, user_id)
