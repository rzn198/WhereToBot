import re
import time
from loguru import logger
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from telegram.error import TelegramError
from shared.config import config
from db.tables import Tables
from bot.middleware.stats import update_user_stats
from bot.utils.messenger import edit_or_send


UNANSWERED_LIMIT = 3


def escape_md(text: str) -> str:
    return re.sub(r'([_*`\[\]])', r'\\\1', text)


async def _require_admin(update: Update) -> bool:
    user = update.effective_user
    if not user or user.id != config['telegram']['admin_user_id']:
        if update.callback_query:
            await update.callback_query.answer('Нет доступа', show_alert=True)
        return False
    return True


async def _edit_contact_window(
    context,
    chat_id: int,
    menu_message_id: int | None,
    text: str,
    keyboard: InlineKeyboardMarkup,
) -> None:
    """Обёртка для обратной совместимости — делегирует в edit_or_send."""
    await edit_or_send(
        context=context,
        chat_id=chat_id,
        text=text,
        keyboard=keyboard,
    )


async def handle_contact_inline(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    await query.answer()

    pool = context.bot_data.get('pool')
    user = update.effective_user
    chat_id = update.effective_chat.id
    menu_message_id = context.user_data.get('menu_message_id')

    await update_user_stats(pool, update)

    row = await pool.fetchrow(
        f"SELECT unanswered_count, is_banned FROM {Tables.CONVERSATIONS} "
        f"WHERE user_id = $1",
        user.id
    )

    if row and row['is_banned']:
        await _edit_contact_window(
            context=context,
            chat_id=chat_id,
            menu_message_id=menu_message_id,
            text="🚫 *Связь с автором недоступна*\n\nВы заблокированы администратором.",
            keyboard=InlineKeyboardMarkup([[
                InlineKeyboardButton('🏠 В меню', callback_data='nav:menu')
            ]]),
        )
        return

    unanswered = row['unanswered_count'] if row else 0

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton('✏️ Написать сообщение', callback_data='contact:write')],
        [InlineKeyboardButton('🏠 В меню', callback_data='nav:menu')],
    ])

    await _edit_contact_window(
        context=context,
        chat_id=chat_id,
        menu_message_id=menu_message_id,
        text=(
            "✉️ *Связь с автором*\n\n"
            "Здесь вы можете написать автору бота.\n"
            f"Ваши сообщения без ответа: {unanswered} / {UNANSWERED_LIMIT}"
        ),
        keyboard=keyboard,
    )


async def handle_contact_write(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    await query.answer()

    pool = context.bot_data.get('pool')
    user = update.effective_user

    row = await pool.fetchrow(
        f"SELECT is_banned FROM {Tables.CONVERSATIONS} WHERE user_id = $1",
        user.id
    )

    if row and row['is_banned']:
        await query.answer('Вы заблокированы администратором.', show_alert=True)
        return

    context.user_data['contact_mode'] = True

    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="✏️ Напишите ваше сообщение автору.\nДля отмены отправьте /cancel"
    )

    logger.info(f"CONTACT_MODE_ON | user_id={user.id}")


async def handle_user_message_to_author(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    pool = context.bot_data.get('pool')
    user = update.effective_user
    text = update.message.text

    last_msg_time = context.user_data.get('last_contact_time', 0)
    if time.time() - last_msg_time < 2.0:
        await update.message.reply_text(
            "⏳ Пожалуйста, не отправляйте сообщения так быстро."
        )
        return
    context.user_data['last_contact_time'] = time.time()

    result = await pool.fetchrow(
        f"""
        INSERT INTO {Tables.CONVERSATIONS} (user_id, unanswered_count)
        VALUES ($1, 1)
        ON CONFLICT (user_id) DO UPDATE
            SET unanswered_count = {Tables.CONVERSATIONS}.unanswered_count + 1
        WHERE {Tables.CONVERSATIONS}.unanswered_count < $2
          AND {Tables.CONVERSATIONS}.is_banned = FALSE
        RETURNING unanswered_count
        """,
        user.id, UNANSWERED_LIMIT
    )

    if not result:
        await update.message.reply_text("⏳ Лимит сообщений достигнут или вы заблокированы.")
        context.user_data['contact_mode'] = False
        return

    msg_id = await pool.fetchval(
        f"""
        INSERT INTO {Tables.CONVERSATION_MESSAGES} (user_id, direction, text)
        VALUES ($1, 'incoming', $2)
        RETURNING id
        """,
        user.id, text
    )

    await update.message.reply_text("✅ Сообщение отправлено автору. Ожидайте ответа.")

    admin_id = config['telegram']['admin_user_id']
    username = f"@{user.username}" if user.username else f"id{user.id}"

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton('↩️ Ответить', callback_data=f'admin_reply:{user.id}'),
        InlineKeyboardButton('🚫 Бан', callback_data=f'admin_ban:{user.id}'),
    ]])

    try:
        await context.bot.send_message(
            chat_id=admin_id,
            text=f"📨 Новое сообщение от {username} ({user.id}):\n\n{text}",
            reply_markup=keyboard,
        )
    except TelegramError as e:
        logger.error(f"Failed to forward message to admin: {e}")

    logger.info(f"MSG_TO_AUTHOR | user_id={user.id} | msg_id={msg_id} | text={text[:50]!r}")


async def handle_cancel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    for key in ['new_event_mode', 'new_event_text', 'new_event_date',
                'alert_mode', 'alert_text', 'alert_source',
                'alert_msg_sid', 'alert_scheduled_at',
                'alert_event_ids', 'alert_event_index']:
        context.user_data.pop(key, None)

    if context.user_data.get('contact_mode'):
        context.user_data['contact_mode'] = False
        await update.message.reply_text("Отменено.")
    elif context.user_data.get('reply_to_user_id'):
        context.user_data['reply_to_user_id'] = None
        await update.message.reply_text("Отменено.")
    else:
        await update.message.reply_text("Нечего отменять.")
