import asyncio
import os
import sys

if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

os.environ.setdefault('APP_ENV', 'production')

from loguru import logger
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    TypeHandler,
    filters,
)

from shared.logging import setup_logging
from shared.config import config
from db.connection import init_db_pool, close_db_pool
from db.validators import validate_config_tables
from bot.health import start_health_server
from bot.scheduler import start_bot_scheduler
from bot.middleware.session import restore_session, save_session
from bot.middleware.locks import get_user_lock
from bot.handlers.start import handle_start, show_main_menu
from bot.handlers.events import handle_filter
from bot.handlers.navigation import handle_navigation
from bot.handlers.favorites import handle_favorites
from bot.handlers.hidden import handle_hide
from bot.handlers.donate import handle_donate
from bot.handlers.alert_dismiss import handle_alert_dismiss
from bot.handlers.contact import (
    handle_contact_inline,
    handle_contact_write,
    handle_user_message_to_author,
    handle_cancel,
)
from bot.handlers.admin_dialogs import (
    handle_dialogs,
    handle_admin_dialogs_inline,
    handle_admin_open_dialog,
    handle_admin_reply_button,
    handle_admin_reply_message,
    handle_admin_ban,
    handle_admin_ban_confirm,
    handle_admin_ban_cancel,
    handle_admin_unban,
)
from bot.handlers.admin_duplicates import (
    handle_admin_duplicates,
    handle_dedup_action,
)
from bot.handlers.admin_panel import (
    handle_admin_panel,
    handle_admin_restart,
    handle_admin_restart_confirm,
    handle_admin_cleanup,
    handle_admin_cleanup_confirm,
    handle_admin_hidden_events,
    handle_admin_hidden_nav,
    handle_admin_hidden_date,
    handle_admin_hidden_event_nav,
    handle_admin_hidden_action,
    handle_admin_alert,
    handle_admin_alert_new,
    handle_admin_alert_from_db,
    handle_admin_alert_send_now,
    handle_admin_alert_schedule,
    handle_admin_alert_confirm,
    handle_admin_alert_pick_nav,
    handle_admin_new_event,
    handle_admin_new_event_confirm,
    handle_admin_new_event_save,
    handle_admin_text_input,
)
from bot.handlers.calendar import (
    handle_calendar,
    handle_calendar_nav,
)

from bot.handlers.cinema import (
    handle_cinema_menu,
    handle_cinema_filter,
    handle_cinema_nav,
    handle_cinema_sessions,
    handle_cinema_pick,
    handle_cinema_calendar,
    handle_cinema_calendar_nav,
    handle_cinema_desc,
    handle_cinema_back_to_movie,
)


async def _session_middleware(update: Update, context) -> None:
    user_id = update.effective_user.id if update.effective_user else None
    if not user_id:
        return

    lock = await get_user_lock(user_id)
    await lock.acquire()

    try:
        await restore_session(context, user_id)
    except Exception:
        lock.release()
        raise


async def _release_lock_middleware(update: Update, context) -> None:
    user_id = update.effective_user.id if update.effective_user else None
    if not user_id:
        return

    lock = await get_user_lock(user_id)
    if lock.locked():
        lock.release()
        logger.debug(f"LOCK_RELEASED | user_id={user_id}")


async def _route_text_message(
    update: Update,
    context,
) -> None:
    user_id = update.effective_user.id
    admin_id = config['telegram']['admin_user_id']

    if user_id == admin_id and context.user_data.get('reply_to_user_id'):
        await handle_admin_reply_message(update, context)
        return

    if user_id == admin_id:
        handled = await handle_admin_text_input(update, context)
        if handled:
            return

    if context.user_data.get('contact_mode'):
        await handle_user_message_to_author(update, context)
        return

    await update.message.reply_text(
        "Нажмите /start для начала работы бота"
    )


async def main():
    setup_logging('bot')
    logger.info(f"Bot starting | env={config['env']}")

    pool = await init_db_pool()
    await validate_config_tables(pool)

    health_runner = None
    if not config['is_local']:
        health_runner = await start_health_server(port=8080, pool=pool)
    else:
        logger.info("Health server skipped | local env")

    tg = config['telegram']
    app = (
        ApplicationBuilder()
        .token(tg['bot_token'])
        .concurrent_updates(True)
        .build()
    )

    app.bot_data['pool'] = pool

    bot_scheduler = start_bot_scheduler(app.bot, pool)

    app.add_handler(TypeHandler(Update, _session_middleware), group=-1)

    # ----------------------------------------------------------
    # Регистрация handlers — порядок важен
    # ----------------------------------------------------------

    app.add_handler(CommandHandler('start', handle_start))
    app.add_handler(CommandHandler('cancel', handle_cancel))
    app.add_handler(CommandHandler('dialogs', handle_dialogs))

    app.add_handler(CallbackQueryHandler(handle_calendar, pattern='^filter:calendar$'))
    app.add_handler(CallbackQueryHandler(handle_calendar_nav, pattern='^calendar_nav:'))
    app.add_handler(CallbackQueryHandler(handle_filter, pattern='^calendar_date:'))

    app.add_handler(CallbackQueryHandler(handle_filter, pattern='^filter:'))

    app.add_handler(CallbackQueryHandler(handle_navigation, pattern='^nav:'))

    app.add_handler(CallbackQueryHandler(handle_favorites, pattern='^fav:'))

    app.add_handler(CallbackQueryHandler(handle_hide, pattern='^hide:'))

    app.add_handler(CallbackQueryHandler(handle_contact_inline, pattern='^contact:start$'))
    app.add_handler(CallbackQueryHandler(handle_contact_write, pattern='^contact:write$'))

    app.add_handler(CallbackQueryHandler(handle_admin_reply_button, pattern='^admin_reply:'))

    app.add_handler(CallbackQueryHandler(handle_admin_ban, pattern=r'^admin_ban:\d+$'))
    app.add_handler(CallbackQueryHandler(handle_admin_ban_confirm, pattern='^admin_ban_confirm:'))
    app.add_handler(CallbackQueryHandler(handle_admin_ban_cancel, pattern='^admin_ban_cancel$'))
    app.add_handler(CallbackQueryHandler(handle_admin_unban, pattern='^admin_unban:'))

    app.add_handler(CallbackQueryHandler(handle_admin_dialogs_inline, pattern='^admin:dialogs$'))
    app.add_handler(CallbackQueryHandler(handle_admin_open_dialog, pattern='^admin_dialog:'))

    app.add_handler(CallbackQueryHandler(handle_admin_duplicates, pattern='^admin:duplicates$'))
    app.add_handler(CallbackQueryHandler(handle_dedup_action, pattern='^dedup:'))

    app.add_handler(CallbackQueryHandler(handle_admin_panel, pattern='^admin_panel:main$'))

    app.add_handler(CallbackQueryHandler(handle_admin_restart, pattern='^admin_panel:restart$'))
    app.add_handler(CallbackQueryHandler(handle_admin_restart_confirm, pattern='^admin_panel:restart_confirm$'))

    app.add_handler(CallbackQueryHandler(handle_admin_cleanup, pattern='^admin_panel:cleanup$'))
    app.add_handler(CallbackQueryHandler(handle_admin_cleanup_confirm, pattern='^admin_panel:cleanup_confirm$'))

    app.add_handler(CallbackQueryHandler(handle_admin_hidden_events, pattern='^admin_panel:hidden_events$'))
    app.add_handler(CallbackQueryHandler(handle_admin_hidden_nav, pattern='^admin_hidden_nav:'))
    app.add_handler(CallbackQueryHandler(handle_admin_hidden_date, pattern='^admin_hidden_cal:'))
    app.add_handler(CallbackQueryHandler(handle_admin_hidden_event_nav, pattern='^admin_hidden_event:'))
    app.add_handler(CallbackQueryHandler(handle_admin_hidden_action, pattern='^admin_hidden:'))

    app.add_handler(CallbackQueryHandler(handle_admin_alert, pattern='^admin_panel:alert$'))
    app.add_handler(CallbackQueryHandler(handle_admin_alert_new, pattern='^admin_alert:new$'))
    app.add_handler(CallbackQueryHandler(handle_admin_alert_from_db, pattern='^admin_alert:from_db$'))
    app.add_handler(CallbackQueryHandler(handle_admin_alert_send_now, pattern='^admin_alert:send_now$'))
    app.add_handler(CallbackQueryHandler(handle_admin_alert_schedule, pattern='^admin_alert:schedule$'))
    app.add_handler(CallbackQueryHandler(handle_admin_alert_confirm, pattern='^admin_alert:confirm$'))
    app.add_handler(CallbackQueryHandler(handle_admin_alert_pick_nav, pattern='^admin_alert_pick:'))

    app.add_handler(CallbackQueryHandler(handle_admin_new_event, pattern='^admin_panel:new_event$'))
    app.add_handler(CallbackQueryHandler(handle_admin_new_event_confirm, pattern='^admin_new_event:confirm$'))
    app.add_handler(CallbackQueryHandler(handle_admin_new_event_save, pattern='^admin_new_event:save$'))

    app.add_handler(CallbackQueryHandler(handle_cinema_menu, pattern='^cinema:menu$'))
    app.add_handler(CallbackQueryHandler(handle_cinema_calendar, pattern='^cinema:calendar$'))
    app.add_handler(CallbackQueryHandler(handle_cinema_calendar_nav, pattern='^cinema_cal_nav:'))
    app.add_handler(CallbackQueryHandler(handle_cinema_filter, pattern='^cinema:(today|tomorrow|date:.+)$'))
    app.add_handler(CallbackQueryHandler(handle_cinema_sessions, pattern='^cinema:sessions:'))
    app.add_handler(CallbackQueryHandler(handle_cinema_pick, pattern='^cinema:pick:'))
    app.add_handler(CallbackQueryHandler(handle_cinema_nav, pattern='^cinema:(prev|next|counter)$'))
    app.add_handler(CallbackQueryHandler(handle_cinema_desc, pattern='^cinema:desc:'))
    app.add_handler(CallbackQueryHandler(handle_cinema_back_to_movie, pattern='^cinema:back_to_movie:'))

    app.add_handler(CallbackQueryHandler(handle_alert_dismiss, pattern='^alert:dismiss$'))

    app.add_handler(MessageHandler(
        filters.TEXT & filters.ChatType.PRIVATE,
        _route_text_message,
    ))

    app.add_handler(TypeHandler(Update, _release_lock_middleware), group=999)

    logger.info("Handlers registered | concurrent_updates=True")

    try:
        logger.info("Bot polling started")
        await app.initialize()
        await app.start()
        await app.updater.start_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )
        await asyncio.Event().wait()

    finally:
        logger.info("Bot stopping...")
        bot_scheduler.shutdown()
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        if health_runner:
            await health_runner.cleanup()
        await close_db_pool(pool)
        logger.info("Bot stopped")


if __name__ == '__main__':
    asyncio.run(main())
