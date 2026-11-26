"""Переходы из пуша: «❤️ В запланированное» и ссылка поста (/start fav_<id>) открывают избранное новым сообщением внизу чата."""
from loguru import logger
from telegram import Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from bot.handlers.events import _winner_ref, load_event_ids, open_filter
from bot.utils.links import FAV_START_RE
from db.tables import Tables

MISSING_TEXT = 'Этого события уже нет в запланированном'


async def drop_menu(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, message_ids) -> None:
    """Удаляет перечисленные сообщения (пуш, команду /start, старое меню) и забывает меню-сообщение: следующий экран
    придёт новым сообщением внизу, а не правкой меню где-то выше по чату."""
    for message_id in dict.fromkeys(m for m in message_ids if m):
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        except TelegramError as e:
            logger.warning(f"PUSH_LINK_DELETE_ERROR | msg_id={message_id} | {e}")
    context.user_data.pop('menu_message_id', None)
    context.user_data.pop('message_type', None)
    context.user_data.pop('expanded', None)
    pool = context.bot_data.get('pool')
    if pool:
        try:
            await pool.execute(f"UPDATE {Tables.USER_STATS} SET menu_message_id = NULL WHERE user_id = $1", user_id)
        except Exception as e:
            logger.warning(f"PUSH_LINK_RESET_ERROR | user_id={user_id} | {type(e).__name__}: {e}")


def parse_start_payload(context: ContextTypes.DEFAULT_TYPE) -> int | None:
    """/start fav_<id> → id поста; всё остальное — None."""
    args = getattr(context, 'args', None) or []
    m = FAV_START_RE.match(args[0]) if len(args) == 1 and isinstance(args[0], str) else None
    return int(m.group(1)) if m else None


async def open_favorite_card(update: Update, context: ContextTypes.DEFAULT_TYPE, ref: int) -> None:
    """Ссылка поста из пуша: список избранного, открытый на карточке ref (после дедупа — на победителе).
    Карточки в избранном нет (⭐ снята, пост скрыт или удалён) — короткая пометка и общий список."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    pool = context.bot_data.get('pool')
    message = update.message
    await drop_menu(context, chat_id, user_id, [message.message_id if message else None,
                                                context.user_data.get('menu_message_id')])
    in_favorites = await load_event_ids(pool, user_id, 'favorites')
    target = next((r for r in (ref, await _winner_ref(pool, ref)) if r in in_favorites), None)
    logger.info(f"PUSH_LINK_OPEN | user_id={user_id} | ref={ref} | found={target is not None}")
    if target is None:
        try:
            await context.bot.send_message(chat_id=chat_id, text=MISSING_TEXT)
        except TelegramError as e:
            logger.warning(f"PUSH_LINK_NOTICE_ERROR | {e}")
    await open_filter(update, context, 'favorites', start_ref=target)
