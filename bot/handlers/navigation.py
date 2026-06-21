import asyncio
from loguru import logger
from telegram import Update
from telegram.ext import ContextTypes

from bot.handlers.start import show_main_menu
from bot.handlers.events import show_current_event

DEBOUNCE_DELAY = 0.3  # seconds


async def handle_navigation(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    pool = context.bot_data.get('pool')
    data = query.data

    if data == 'nav:menu':
        await query.answer()
        menu_message_id = context.user_data.get('menu_message_id')
        message_type = context.user_data.get('message_type', 'text')
        context.user_data.clear()
        if menu_message_id:
            context.user_data['menu_message_id'] = menu_message_id
            context.user_data['message_type'] = message_type
        await show_main_menu(update, context)
        return

    if data == 'nav:counter':
        await query.answer()
        return

    if data == 'nav:expand':
        await query.answer()
        context.user_data['expanded'] = True
        await show_current_event(update, context, pool)
        return

    event_ids = context.user_data.get('event_ids', [])
    current_index = context.user_data.get('current_index', 0)

    if not event_ids:
        await query.answer('Сессия истекла. Выбери фильтр заново.', show_alert=True)
        return

    if data == 'nav:prev':
        if current_index > 0:
            await query.answer()
            context.user_data['current_index'] = current_index - 1
            context.user_data['expanded'] = False
        else:
            await query.answer('Это первое событие', show_alert=False)
            return

    elif data == 'nav:next':
        if current_index < len(event_ids) - 1:
            await query.answer()
            context.user_data['current_index'] = current_index + 1
            context.user_data['expanded'] = False
        else:
            await query.answer('Это последнее событие', show_alert=False)
            return

    else:
        await query.answer()
        return

    logger.debug(
        f"NAV | user_id={update.effective_user.id} | "
        f"action={data} | index={context.user_data['current_index']}"
    )

    existing_task = context.user_data.get('nav_debounce_task')
    if existing_task and not existing_task.done():
        existing_task.cancel()
        logger.debug(f"NAV_DEBOUNCE_CANCELLED | user_id={update.effective_user.id}")

    async def debounced_nav():
        try:
            await asyncio.sleep(DEBOUNCE_DELAY)
            await show_current_event(update, context, pool)
        except asyncio.CancelledError:
            pass

    task = asyncio.create_task(debounced_nav())
    context.user_data['nav_debounce_task'] = task