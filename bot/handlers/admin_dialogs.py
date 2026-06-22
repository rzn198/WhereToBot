from loguru import logger
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from telegram.error import TelegramError

from db.tables import Tables
from bot.middleware.admin import admin_only
from bot.handlers.contact import _require_admin, _edit_contact_window, escape_md


@admin_only
async def handle_dialogs(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    pool = context.bot_data.get('pool')

    rows = await pool.fetch(
        f"""
        SELECT
            c.user_id,
            c.unanswered_count,
            c.is_banned,
            c.created_at,
            (
                SELECT text FROM {Tables.CONVERSATION_MESSAGES}
                WHERE user_id = c.user_id AND direction = 'incoming'
                ORDER BY created_at DESC
                LIMIT 1
            ) as last_message
        FROM {Tables.CONVERSATIONS} c
        ORDER BY c.created_at DESC
        LIMIT 20
        """
    )

    if not rows:
        await update.message.reply_text("📭 Нет диалогов.")
        return

    text = f"📬 Диалоги ({len(rows)}):\n\n"
    buttons = []

    for row in rows:
        user_id = row['user_id']
        count = row['unanswered_count']
        is_banned = row['is_banned']
        last_msg = escape_md((row['last_message'] or '—')[:60])
        status = '🚫 Забанен' if is_banned else f'📩 Неотвеченных: {count}'

        text += f"👤 {user_id}\n💬 {last_msg}\n{status}\n\n"

        row_buttons = []
        if not is_banned:
            row_buttons.append(
                InlineKeyboardButton('↩️ Ответить', callback_data=f'admin_reply:{user_id}')
            )
        row_buttons.append(
            InlineKeyboardButton(
                '✅ Разбан' if is_banned else '🚫 Бан',
                callback_data=f'admin_unban:{user_id}' if is_banned else f'admin_ban:{user_id}'
            )
        )
        buttons.append(row_buttons)

    await update.message.reply_text(text=text, reply_markup=InlineKeyboardMarkup(buttons))
    logger.info(f"DIALOGS_VIEW | admin_id={update.effective_user.id}")


async def handle_admin_dialogs_inline(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    await query.answer()

    if not await _require_admin(update):
        return

    pool = context.bot_data.get('pool')
    chat_id = update.effective_chat.id
    menu_message_id = context.user_data.get('menu_message_id')

    rows = await pool.fetch(
        f"""
        SELECT
            c.user_id,
            c.unanswered_count,
            c.is_banned,
            (
                SELECT text FROM {Tables.CONVERSATION_MESSAGES}
                WHERE user_id = c.user_id AND direction = 'incoming'
                ORDER BY created_at DESC
                LIMIT 1
            ) as last_message
        FROM {Tables.CONVERSATIONS} c
        ORDER BY c.created_at DESC
        LIMIT 20
        """
    )

    if not rows:
        await _edit_contact_window(
            context=context,
            chat_id=chat_id,
            menu_message_id=menu_message_id,
            text="📭 *Диалогов пока нет.*",
            keyboard=InlineKeyboardMarkup([[
                InlineKeyboardButton('🏠 В меню', callback_data='nav:menu')
            ]]),
        )
        return

    text = "📬 *Диалоги*\n\n"
    buttons = []

    for row in rows:
        user_id = row['user_id']
        count = row['unanswered_count']
        is_banned = row['is_banned']
        last_msg = escape_md((row['last_message'] or '—')[:50])
        status = '🚫' if is_banned else f'📩 {count}'

        text += f"👤 `{user_id}` {status}\n_{last_msg}_\n\n"

        row_buttons = [
            InlineKeyboardButton('💬 Открыть', callback_data=f'admin_dialog:{user_id}'),
        ]
        if is_banned:
            row_buttons.append(
                InlineKeyboardButton('✅ Разбан', callback_data=f'admin_unban:{user_id}')
            )
        else:
            row_buttons.append(
                InlineKeyboardButton('🚫 Бан', callback_data=f'admin_ban:{user_id}')
            )
        buttons.append(row_buttons)

    buttons.append([InlineKeyboardButton('🏠 В меню', callback_data='nav:menu')])

    await _edit_contact_window(
        context=context,
        chat_id=chat_id,
        menu_message_id=menu_message_id,
        text=text,
        keyboard=InlineKeyboardMarkup(buttons),
    )


async def handle_admin_open_dialog(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    await query.answer()

    if not await _require_admin(update):
        return

    pool = context.bot_data.get('pool')
    chat_id = update.effective_chat.id
    menu_message_id = context.user_data.get('menu_message_id')

    parts = query.data.split(':')
    if len(parts) != 2 or not parts[1].isdigit():
        await query.answer('Неверный запрос', show_alert=True)
        return

    user_id = int(parts[1])

    conv = await pool.fetchrow(
        f"SELECT is_banned, unanswered_count FROM {Tables.CONVERSATIONS} WHERE user_id = $1",
        user_id
    )

    messages = await pool.fetch(
        f"""
        SELECT direction, text, created_at
        FROM {Tables.CONVERSATION_MESSAGES}
        WHERE user_id = $1
        ORDER BY created_at DESC
        LIMIT 10
        """,
        user_id
    )

    history = ""
    for msg in reversed(messages):
        time_str = msg['created_at'].strftime('%H:%M')
        msg_text = escape_md(msg['text'][:100])
        if msg['direction'] == 'incoming':
            history += f"👤 _{msg_text}_\n   `{time_str}`\n\n"
        else:
            history += f"📩 {msg_text}\n   `{time_str}`\n\n"

    if not history:
        history = "_Сообщений пока нет_\n\n"

    is_banned = conv['is_banned'] if conv else False
    text = f"💬 *Диалог с* `{user_id}`\n\n{history}──────────────────"
    context.user_data['dialog_user_id'] = user_id

    buttons = []
    if not is_banned:
        buttons.append([
            InlineKeyboardButton('↩️ Ответить', callback_data=f'admin_reply:{user_id}'),
            InlineKeyboardButton('🚫 Бан', callback_data=f'admin_ban:{user_id}'),
        ])
    else:
        buttons.append([
            InlineKeyboardButton('✅ Разбан', callback_data=f'admin_unban:{user_id}'),
        ])

    buttons.append([
        InlineKeyboardButton('◀️ К диалогам', callback_data='admin:dialogs'),
    ])

    await _edit_contact_window(
        context=context,
        chat_id=chat_id,
        menu_message_id=menu_message_id,
        text=text,
        keyboard=InlineKeyboardMarkup(buttons),
    )

    logger.info(f"DIALOG_OPENED | admin_id={update.effective_user.id} | user_id={user_id}")


async def handle_admin_reply_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    await query.answer()

    if not await _require_admin(update):
        return

    parts = query.data.split(':')
    if len(parts) != 2 or not parts[1].isdigit():
        await query.answer('Неверный запрос', show_alert=True)
        return

    user_id = int(parts[1])
    context.user_data['reply_to_user_id'] = user_id

    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"↩️ Напишите ответ пользователю {user_id}.\nДля отмены отправьте /cancel"
    )

    logger.info(f"ADMIN_REPLY_MODE | admin_id={update.effective_user.id} | target_user={user_id}")


async def handle_admin_reply_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    pool = context.bot_data.get('pool')
    admin = update.effective_user
    text = update.message.text
    target_user_id = context.user_data.get('reply_to_user_id')

    if not target_user_id:
        return

    await pool.execute(
        f"""
        INSERT INTO {Tables.CONVERSATION_MESSAGES} (user_id, direction, text)
        VALUES ($1, 'outgoing', $2)
        """,
        target_user_id, text
    )

    await pool.execute(
        f"UPDATE {Tables.CONVERSATIONS} SET unanswered_count = 0 WHERE user_id = $1",
        target_user_id
    )

    context.user_data['reply_to_user_id'] = None

    try:
        await context.bot.send_message(
            chat_id=target_user_id,
            text=f"📩 Ответ от автора:\n\n{text}"
        )
        await update.message.reply_text(f"✅ Ответ отправлен пользователю {target_user_id}.")
    except TelegramError as e:
        logger.error(f"Failed to send reply to user {target_user_id}: {e}")
        await update.message.reply_text(f"❌ Не удалось отправить ответ: {e}")

    logger.info(f"ADMIN_REPLY_SENT | admin_id={admin.id} | target_user={target_user_id} | text={text[:50]!r}")


async def handle_admin_ban(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    await query.answer()

    if not await _require_admin(update):
        return

    parts = query.data.split(':')
    if len(parts) != 2 or not parts[1].isdigit():
        await query.answer('Неверный запрос', show_alert=True)
        return

    user_id = int(parts[1])
    admin_id = update.effective_user.id

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton('✅ Подтвердить бан', callback_data=f'admin_ban_confirm:{user_id}'),
        InlineKeyboardButton('❌ Отмена', callback_data='admin_ban_cancel'),
    ]])

    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"⚠️ Заблокировать пользователя {user_id}?",
        reply_markup=keyboard,
    )

    logger.info(f"BAN_CONFIRM_SHOWN | admin_id={admin_id} | user_id={user_id}")


async def handle_admin_ban_confirm(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    await query.answer()

    if not await _require_admin(update):
        return

    pool = context.bot_data.get('pool')

    parts = query.data.split(':')
    if len(parts) != 2 or not parts[1].isdigit():
        await query.answer('Неверный запрос', show_alert=True)
        return

    user_id = int(parts[1])
    admin_id = update.effective_user.id

    await pool.execute(
        f"""
        INSERT INTO {Tables.CONVERSATIONS} (user_id, is_banned)
        VALUES ($1, TRUE)
        ON CONFLICT (user_id) DO UPDATE SET is_banned = TRUE
        """,
        user_id
    )

    await query.message.edit_text(f"🚫 Пользователь {user_id} заблокирован.")
    logger.info(f"USER_BANNED | admin_id={admin_id} | user_id={user_id}")


async def handle_admin_ban_cancel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    await query.answer()
    await query.message.edit_text("❌ Бан отменён.")


async def handle_admin_unban(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    await query.answer()

    if not await _require_admin(update):
        return

    pool = context.bot_data.get('pool')

    parts = query.data.split(':')
    if len(parts) != 2 or not parts[1].isdigit():
        await query.answer('Неверный запрос', show_alert=True)
        return

    user_id = int(parts[1])
    admin_id = update.effective_user.id

    await pool.execute(
        f"UPDATE {Tables.CONVERSATIONS} SET is_banned = FALSE WHERE user_id = $1",
        user_id
    )

    await query.message.edit_text(f"✅ Пользователь {user_id} разблокирован.")
    logger.info(f"USER_UNBANNED | admin_id={admin_id} | user_id={user_id}")
