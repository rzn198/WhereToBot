from loguru import logger
from db.tables import Tables


async def restore_session(context, user_id: int) -> None:

    context.user_data['user_id'] = user_id

    if context.user_data.get('menu_message_id'):
        return

    pool = context.bot_data.get('pool')
    if not pool:
        return

    try:
        row = await pool.fetchrow(f"""
            SELECT menu_message_id, menu_message_type
            FROM {Tables.USER_STATS}
            WHERE user_id = $1
        """, user_id)

        if row and row['menu_message_id']:
            context.user_data['menu_message_id'] = row['menu_message_id']
            context.user_data['message_type'] = row['menu_message_type'] or 'text'
            logger.debug(
                f"SESSION_RESTORED | user_id={user_id} | "
                f"msg_id={row['menu_message_id']} | "
                f"type={row['menu_message_type']}"
            )
    except Exception as e:
        logger.error(f"SESSION_RESTORE_ERROR | user_id={user_id} | {e}")


async def save_session(context, user_id: int | None) -> None:
    if not user_id:
        user_id = context.user_data.get('user_id')
    if not user_id:
        return

    pool = context.bot_data.get('pool')
    if not pool:
        return

    menu_message_id = context.user_data.get('menu_message_id')
    message_type = context.user_data.get('message_type', 'text')

    if not menu_message_id:
        return

    try:
        await pool.execute(f"""
            UPDATE {Tables.USER_STATS}
            SET menu_message_id   = $1,
                menu_message_type = $2
            WHERE user_id = $3
        """, menu_message_id, message_type, user_id)
        logger.debug(
            f"SESSION_SAVED | user_id={user_id} | "
            f"msg_id={menu_message_id} | type={message_type}"
        )
    except Exception as e:
        logger.error(f"SESSION_SAVE_ERROR | user_id={user_id} | {e}")
