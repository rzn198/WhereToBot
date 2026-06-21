import asyncio
import re
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
import json
import calendar
from datetime import timezone
from db.cleanup import run_cleanup
from loguru import logger
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.error import TelegramError
from telegram.ext import ContextTypes
from shared.config import config
from db.tables import Tables
from bot.handlers.contact import _require_admin, _edit_contact_window, escape_md


BELGRADE_TZ = ZoneInfo('Europe/Belgrade')
UTC_TZ = ZoneInfo('UTC')
MONTHS_RU = [
    '', 'Январь', 'Февраль', 'Март', 'Апрель', 'Май', 'Июнь',
    'Июль', 'Август', 'Сентябрь', 'Октябрь', 'Ноябрь', 'Декабрь'
]

def _admin_back_button() -> list:
    return [InlineKeyboardButton('◀️ В админку', callback_data='admin_panel:main')]


def _menu_back_button() -> list:
    return [InlineKeyboardButton('🏠 В меню', callback_data='nav:menu')]


async def handle_admin_panel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    await query.answer()

    if not await _require_admin(update):
        return

    chat_id = update.effective_chat.id
    menu_message_id = context.user_data.get('menu_message_id')

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton('🔄 Рестарт', callback_data='admin_panel:restart')],
        [InlineKeyboardButton('🧹 Очистка', callback_data='admin_panel:cleanup')],
        [InlineKeyboardButton('👁 Скрытые события', callback_data='admin_panel:hidden_events')],
        [InlineKeyboardButton('📢 Алерт', callback_data='admin_panel:alert')],
        [InlineKeyboardButton('✏️ Новое сообщение', callback_data='admin_panel:new_event')],
        [InlineKeyboardButton('🏠 В меню', callback_data='nav:menu')],
    ])

    await _edit_contact_window(
        context=context,
        chat_id=chat_id,
        menu_message_id=menu_message_id,
        text="⚙️ *Панель администратора*",
        keyboard=keyboard,
    )


async def handle_admin_restart(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    await query.answer()

    if not await _require_admin(update):
        return

    chat_id = update.effective_chat.id
    menu_message_id = context.user_data.get('menu_message_id')

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton('✅ Подтвердить', callback_data='admin_panel:restart_confirm'),
            InlineKeyboardButton('❌ Отмена', callback_data='admin_panel:main'),
        ],
    ])

    await _edit_contact_window(
        context=context,
        chat_id=chat_id,
        menu_message_id=menu_message_id,
        text=(
            "⚠️ *Перезапустить бота?*\n\n"
            "Бот будет недоступен ~10 секунд.\n"
            "Docker автоматически поднимет его."
        ),
        keyboard=keyboard,
    )


async def handle_admin_restart_confirm(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    await query.answer()

    if not await _require_admin(update):
        return

    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="🔄 Перезапускаюсь..."
    )

    logger.info(f"RESTART | admin_id={update.effective_user.id}")

    asyncio.get_event_loop().stop()

async def handle_admin_cleanup(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    await query.answer()

    if not await _require_admin(update):
        return

    chat_id = update.effective_chat.id
    menu_message_id = context.user_data.get('menu_message_id')
    cfg = config.get('cleanup', {})

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton('✅ Подтвердить', callback_data='admin_panel:cleanup_confirm'),
            InlineKeyboardButton('❌ Отмена', callback_data='admin_panel:main'),
        ],
    ])

    await _edit_contact_window(
        context=context,
        chat_id=chat_id,
        menu_message_id=menu_message_id,
        text=(
            "⚠️ *Запустить очистку БД?*\n\n"
            "Будут удалены:\n"
            f"• события старше {cfg.get('event_cleanup_days', 30)} дней\n"
            f"• события без даты старше {cfg.get('no_date_cleanup_days', 7)} дней"
        ),
        keyboard=keyboard,
    )


async def handle_admin_cleanup_confirm(
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

    result = await run_cleanup(pool)

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton('◀️ В админку', callback_data='admin_panel:main')],
    ])

    await _edit_contact_window(
        context=context,
        chat_id=chat_id,
        menu_message_id=menu_message_id,
        text=(
            "✅ *Очистка выполнена:*\n\n"
            f"• удалено {result['dated']} событий с датой\n"
            f"• удалено {result['no_date']} событий без даты"
        ),
        keyboard=keyboard,
    )

    logger.info(f"CLEANUP_MANUAL | admin_id={update.effective_user.id} | result={result}")


async def handle_admin_hidden_events(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    await query.answer()

    if not await _require_admin(update):
        return

    today = date.today()
    context.user_data['admin_hidden_year'] = today.year
    context.user_data['admin_hidden_month'] = today.month

    await _show_admin_hidden_calendar(update, context)


async def _show_admin_hidden_calendar(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    pool = context.bot_data.get('pool')
    chat_id = update.effective_chat.id
    menu_message_id = context.user_data.get('menu_message_id')

    year = context.user_data.get('admin_hidden_year', date.today().year)
    month = context.user_data.get('admin_hidden_month', date.today().month)

    row = await pool.fetchrow(f"""
            SELECT
                array_agg(DISTINCT date) as dates,
                MAX(date) as max_date
            FROM {Tables.MESSAGES}
            WHERE date IS NOT NULL
            AND date >= CURRENT_DATE
        """)
    dates_with_events = set(row['dates'] or [])
    max_date = row['max_date'] if row and row['max_date'] else date.today()

    keyboard = _build_calendar(
        year=year,
        month=month,
        dates_with_events=dates_with_events,
        max_date=max_date,
        callback_prefix='admin_hidden_cal',
        nav_prefix='admin_hidden_nav',
    )
    keyboard.append([InlineKeyboardButton('◀️ В админку', callback_data='admin_panel:main')])

    await _edit_contact_window(
        context=context,
        chat_id=chat_id,
        menu_message_id=menu_message_id,
        text=f"👁 *Скрытые события — выберите дату:*\n\n{MONTHS_RU[month]} {year}",
        keyboard=InlineKeyboardMarkup(keyboard),
    )


async def handle_admin_hidden_nav(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    await query.answer()

    if not await _require_admin(update):
        return

    parts = query.data.split(':')
    direction = parts[1]

    year = context.user_data.get('admin_hidden_year', date.today().year)
    month = context.user_data.get('admin_hidden_month', date.today().month)

    if direction == 'prev':
        month -= 1
        if month < 1:
            month = 12
            year -= 1
    elif direction == 'next':
        month += 1
        if month > 12:
            month = 1
            year += 1

    context.user_data['admin_hidden_year'] = year
    context.user_data['admin_hidden_month'] = month

    await _show_admin_hidden_calendar(update, context)


async def handle_admin_hidden_date(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    await query.answer()

    if not await _require_admin(update):
        return

    pool = context.bot_data.get('pool')

    try:
        date_str = query.data.split(':', 1)[1]
        selected_date = date.fromisoformat(date_str)
    except (IndexError, ValueError):
        await query.answer('Неверная дата', show_alert=True)
        return

    rows = await pool.fetch(f"""
        SELECT surrogate_id FROM {Tables.MESSAGES}
        WHERE date = $1
        ORDER BY created_at
    """, selected_date)

    if not rows:
        await _edit_contact_window(
            context=context,
            chat_id=update.effective_chat.id,
            menu_message_id=context.user_data.get('menu_message_id'),
            text="🤷 На выбранную дату событий нет.",
            keyboard=InlineKeyboardMarkup([
                [InlineKeyboardButton('📅 Выбрать другую дату', callback_data='admin_panel:hidden_events')],
                [InlineKeyboardButton('◀️ В админку', callback_data='admin_panel:main')],
            ]),
        )
        return

    context.user_data['admin_hidden_event_ids'] = [r['surrogate_id'] for r in rows]
    context.user_data['admin_hidden_event_index'] = 0

    await _show_admin_hidden_event(update, context, pool)


async def _show_admin_hidden_event(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    pool,
) -> None:
    chat_id = update.effective_chat.id
    menu_message_id = context.user_data.get('menu_message_id')
    event_ids = context.user_data.get('admin_hidden_event_ids', [])
    idx = context.user_data.get('admin_hidden_event_index', 0)

    if not event_ids or idx >= len(event_ids):
        return

    surrogate_id = event_ids[idx]

    row = await pool.fetchrow(f"""
        SELECT surrogate_id, text, date, hidden_by_admin
        FROM {Tables.MESSAGES}
        WHERE surrogate_id = $1
    """, surrogate_id)

    if not row:
        return

    status = '🚫 Скрытое' if row['hidden_by_admin'] else '👁 Видимое'
    text = (
        f"📅 {row['date']}\n"
        f"Статус: {status}\n\n"
        f"{escape_md(row['text'][:300])}"
    )

    hide_show_btn = (
        InlineKeyboardButton('✅ Показать для всех', callback_data=f'admin_hidden:show:{surrogate_id}')
        if row['hidden_by_admin'] else
        InlineKeyboardButton('🚫 Скрыть для всех', callback_data=f'admin_hidden:hide:{surrogate_id}')
    )

    nav_row = [
        InlineKeyboardButton('🔙', callback_data='admin_hidden_event:prev'),
        InlineKeyboardButton(f'{idx+1}/{len(event_ids)}', callback_data='admin_hidden_event:counter'),
        InlineKeyboardButton('🔜', callback_data='admin_hidden_event:next'),
    ]

    keyboard = InlineKeyboardMarkup([
        nav_row,
        [hide_show_btn],
        [InlineKeyboardButton('📅 Другая дата', callback_data='admin_panel:hidden_events')],
        [InlineKeyboardButton('◀️ В админку', callback_data='admin_panel:main')],
    ])

    await _edit_contact_window(
        context=context,
        chat_id=chat_id,
        menu_message_id=menu_message_id,
        text=text,
        keyboard=keyboard,
    )


async def handle_admin_hidden_event_nav(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    await query.answer()

    if not await _require_admin(update):
        return

    pool = context.bot_data.get('pool')
    parts = query.data.split(':')
    direction = parts[1]

    event_ids = context.user_data.get('admin_hidden_event_ids', [])
    idx = context.user_data.get('admin_hidden_event_index', 0)

    if direction == 'prev' and idx > 0:
        context.user_data['admin_hidden_event_index'] = idx - 1
    elif direction == 'next' and idx < len(event_ids) - 1:
        context.user_data['admin_hidden_event_index'] = idx + 1

    await _show_admin_hidden_event(update, context, pool)


async def handle_admin_hidden_action(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    await query.answer()

    if not await _require_admin(update):
        return

    pool = context.bot_data.get('pool')
    parts = query.data.split(':')

    if len(parts) < 3 or not parts[2].isdigit():
        await query.answer('Неверный запрос', show_alert=True)
        return

    action = parts[1]
    surrogate_id = int(parts[2])

    if action not in ('hide', 'show'):
        await query.answer('Неверное действие', show_alert=True)
        return

    hidden = action == 'hide'

    hidden = action == 'hide'
    await pool.execute(f"""
        UPDATE {Tables.MESSAGES}
        SET hidden_by_admin = $1
        WHERE surrogate_id = $2
    """, hidden, surrogate_id)

    logger.info(
        f"ADMIN_HIDDEN_ACTION | admin_id={update.effective_user.id} | "
        f"action={action} | surrogate_id={surrogate_id}"
    )

    await _show_admin_hidden_event(update, context, pool)


async def handle_admin_alert(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """REQ-ADMIN-04: подменю алерта."""
    query = update.callback_query
    await query.answer()

    if not await _require_admin(update):
        return

    chat_id = update.effective_chat.id
    menu_message_id = context.user_data.get('menu_message_id')

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton('✏️ Новое', callback_data='admin_alert:new'),
            InlineKeyboardButton('📋 Из базы', callback_data='admin_alert:from_db'),
        ],
        [InlineKeyboardButton('◀️ В админку', callback_data='admin_panel:main')],
    ])

    await _edit_contact_window(
        context=context,
        chat_id=chat_id,
        menu_message_id=menu_message_id,
        text="📢 *Алерт для всех пользователей*\n\nВыберите источник:",
        keyboard=keyboard,
    )


async def handle_admin_alert_new(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    await query.answer()

    if not await _require_admin(update):
        return

    context.user_data['alert_mode'] = 'text'
    context.user_data['alert_source'] = 'new'

    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="✏️ Введите текст алерта.\nДля отмены отправьте /cancel"
    )


async def handle_admin_alert_from_db(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    await query.answer()

    if not await _require_admin(update):
        return

    context.user_data['alert_mode'] = 'date'
    context.user_data['alert_source'] = 'from_db'

    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="📋 Введите дату в формате ДД.ММ.ГГГГ.\nДля отмены отправьте /cancel"
    )


async def handle_admin_alert_send_now(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    await query.answer()

    if not await _require_admin(update):
        return

    context.user_data['alert_scheduled_at'] = datetime.now(UTC_TZ)
    await _show_alert_confirm(update, context)


async def handle_admin_alert_schedule(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    await query.answer()

    if not await _require_admin(update):
        return

    context.user_data['alert_mode'] = 'time'

    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="⏰ Введите время отправки в формате ЧЧ:ММ (по Белграду).\nДля отмены отправьте /cancel"
    )


async def _show_alert_confirm(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    chat_id = update.effective_chat.id
    menu_message_id = context.user_data.get('menu_message_id')
    alert_text = context.user_data.get('alert_text', '')
    scheduled_at = context.user_data.get('alert_scheduled_at')

    belgrade_time = scheduled_at.astimezone(BELGRADE_TZ)
    now_belgrade = datetime.now(BELGRADE_TZ)

    if (belgrade_time - now_belgrade).total_seconds() < 60:
        time_str = "сейчас"
    else:
        time_str = f"{belgrade_time.strftime('%H:%M')} по Белграду"

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton('✅ Подтвердить', callback_data='admin_alert:confirm'),
            InlineKeyboardButton('❌ Отмена', callback_data='admin_panel:main'),
        ],
    ])

    await _edit_contact_window(
        context=context,
        chat_id=chat_id,
        menu_message_id=menu_message_id,
        text=(
            "⚠️ *Подтвердить отправку алерта?*\n\n"
            f"Текст: {escape_md(alert_text[:200])}\n\n"
            f"Время: {time_str}"
        ),
        keyboard=keyboard,
    )


async def handle_admin_alert_confirm(
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

    alert_text = context.user_data.get('alert_text', '').strip()
    scheduled_at = context.user_data.get('alert_scheduled_at')
    source = context.user_data.get('alert_source', 'new')
    msg_surrogate_id = context.user_data.get('alert_msg_sid')

    if not alert_text:
        await _edit_contact_window(
            context=context,
            chat_id=chat_id,
            menu_message_id=menu_message_id,
            text="❌ *Текст алерта не может быть пустым.*",
            keyboard=InlineKeyboardMarkup([[
                InlineKeyboardButton('◀️ В админку', callback_data='admin_panel:main'),
            ]]),
        )
        return

    existing = await pool.fetchval(f"""
        SELECT id FROM {Tables.SCHEDULED_ALERTS}
        WHERE text = $1 AND scheduled_at = $2 AND sent_at IS NULL
        AND created_at > NOW() - INTERVAL '10 seconds'
    """, alert_text, scheduled_at)

    if existing:
        logger.warning(
            f"ALERT_DUPLICATE_PREVENTED | admin_id={update.effective_user.id} | "
            f"existing_id={existing}"
        )
    else:
        await pool.execute(f"""
            INSERT INTO {Tables.SCHEDULED_ALERTS}
            (text, scheduled_at, source, msg_surrogate_id)
            VALUES ($1, $2, $3, $4)
        """, alert_text, scheduled_at, source, msg_surrogate_id)

    for key in ['alert_mode', 'alert_text', 'alert_source', 'alert_msg_sid',
                'alert_scheduled_at', 'alert_event_ids', 'alert_event_index']:
        context.user_data.pop(key, None)

    logger.info(
        f"ALERT_SCHEDULED | admin_id={update.effective_user.id} | "
        f"scheduled_at={scheduled_at} | source={source}"
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton('◀️ В админку', callback_data='admin_panel:main')],
    ])

    await _edit_contact_window(
        context=context,
        chat_id=chat_id,
        menu_message_id=menu_message_id,
        text="✅ *Алерт запланирован.*",
        keyboard=keyboard,
    )


async def handle_admin_new_event(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    await query.answer()

    if not await _require_admin(update):
        return

    context.user_data['new_event_mode'] = 'text'

    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="✏️ Введите текст события.\nДля отмены отправьте /cancel"
    )


async def handle_admin_new_event_confirm(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    await query.answer()

    if not await _require_admin(update):
        return

    chat_id = update.effective_chat.id
    menu_message_id = context.user_data.get('menu_message_id')
    event_text = context.user_data.get('new_event_text', '')
    event_date = context.user_data.get('new_event_date')

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton('✅ Сохранить', callback_data='admin_new_event:save'),
            InlineKeyboardButton('❌ Отмена', callback_data='admin_panel:main'),
        ],
    ])

    await _edit_contact_window(
        context=context,
        chat_id=chat_id,
        menu_message_id=menu_message_id,
        text=(
            "⚠️ *Сохранить событие в БД?*\n\n"
            f"Текст: {escape_md(event_text[:200])}\n\n"
            f"Дата: {event_date}"
        ),
        keyboard=keyboard,
    )


async def handle_admin_new_event_save(
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

    event_text = context.user_data.get('new_event_text', '')
    event_date = context.user_data.get('new_event_date')

    published_at = datetime.now(timezone.utc)

    manual_id = -int(datetime.now(timezone.utc).timestamp() * 1000)

    await pool.execute(f"""
        INSERT INTO {Tables.MESSAGES} (
            id, position, channel_id, text, entities,
            date, published_at
        ) VALUES ($1, $2, $3, $4, $5, $6, $7)
        ON CONFLICT (id, position) DO NOTHING
    """,
        manual_id,
        0,          # position
        0,          # channel_id = 0 для ручных записей
        event_text,
        json.dumps([]),
        event_date,
        published_at,
    )

    context.user_data.pop('new_event_mode', None)
    context.user_data.pop('new_event_text', None)
    context.user_data.pop('new_event_date', None)

    logger.info(
        f"NEW_EVENT_SAVED | admin_id={update.effective_user.id} | "
        f"date={event_date} | text={event_text[:50]!r}"
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton('◀️ В админку', callback_data='admin_panel:main')],
    ])

    await _edit_contact_window(
        context=context,
        chat_id=chat_id,
        menu_message_id=menu_message_id,
        text="✅ *Событие сохранено в БД.*",
        keyboard=keyboard,
    )


async def handle_admin_text_input(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    pool = context.bot_data.get('pool')
    user_id = update.effective_user.id
    admin_id = config['telegram']['admin_user_id']
    text = update.message.text

    if user_id != admin_id:
        return False

    new_event_mode = context.user_data.get('new_event_mode')
    if new_event_mode == 'text':
        context.user_data['new_event_text'] = text
        context.user_data['new_event_mode'] = 'date'
        await update.message.reply_text(
            "📅 Введите дату в формате ДД.ММ.ГГГГ.\n"
            "Для отмены отправьте /cancel"
        )
        return True

    elif new_event_mode == 'date':
        parsed_date = _parse_date_input(text)
        if not parsed_date:
            await update.message.reply_text(
                "❌ Неверный формат даты. Введите в формате ДД.ММ.ГГГГ"
            )
            return True
        context.user_data['new_event_date'] = parsed_date
        context.user_data['new_event_mode'] = None
        await _show_new_event_confirm(update, context)
        return True

    alert_mode = context.user_data.get('alert_mode')
    if alert_mode == 'text':
        context.user_data['alert_text'] = text
        context.user_data['alert_mode'] = None
        await _show_alert_time_choice(update, context)
        return True

    elif alert_mode == 'date':
        parsed_date = _parse_date_input(text)
        if not parsed_date:
            await update.message.reply_text(
                "❌ Неверный формат даты. Введите в формате ДД.ММ.ГГГГ"
            )
            return True
        rows = await pool.fetch(f"""
            SELECT surrogate_id FROM {Tables.MESSAGES}
            WHERE date = $1
            AND hidden_by_admin = FALSE
            ORDER BY created_at
        """, parsed_date)

        if not rows:
            await update.message.reply_text(
                f"❌ На дату {text} событий не найдено."
            )
            return True

        context.user_data['alert_event_ids'] = [r['surrogate_id'] for r in rows]
        context.user_data['alert_event_index'] = 0
        context.user_data['alert_mode'] = None
        await _show_alert_event_picker(update, context, pool)
        return True

    elif alert_mode == 'time':
        parsed_time = _parse_time_input(text)
        if not parsed_time:
            await update.message.reply_text(
                "❌ Неверный формат времени. Введите в формате ЧЧ:ММ"
            )
            return True

        now_belgrade = datetime.now(BELGRADE_TZ)
        scheduled_belgrade = now_belgrade.replace(
            hour=parsed_time[0],
            minute=parsed_time[1],
            second=0,
            microsecond=0,
        )
        if scheduled_belgrade <= now_belgrade:
            scheduled_belgrade += timedelta(days=1)

        context.user_data['alert_scheduled_at'] = scheduled_belgrade.astimezone(UTC_TZ)
        context.user_data['alert_mode'] = None
        await _show_alert_confirm(update, context)
        return True

    return False


async def _show_new_event_confirm(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    chat_id = update.effective_chat.id
    menu_message_id = context.user_data.get('menu_message_id')
    event_text = context.user_data.get('new_event_text', '')
    event_date = context.user_data.get('new_event_date')

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton('✅ Сохранить', callback_data='admin_new_event:save'),
            InlineKeyboardButton('❌ Отмена', callback_data='admin_panel:main'),
        ],
    ])

    text = (
        "⚠️ *Сохранить событие в БД?*\n\n"
        f"Текст: {escape_md(event_text[:200])}\n\n"
        f"Дата: {event_date}"
    )

    if menu_message_id:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=menu_message_id,
            text=text,
            reply_markup=keyboard,
            parse_mode='Markdown',
        )
    else:
        await update.message.reply_text(
            text=text,
            reply_markup=keyboard,
            parse_mode='Markdown',
        )


async def _show_alert_time_choice(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    chat_id = update.effective_chat.id
    menu_message_id = context.user_data.get('menu_message_id')
    alert_text = context.user_data.get('alert_text', '')

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton('📨 Отправить сейчас', callback_data='admin_alert:send_now'),
            InlineKeyboardButton('⏰ Запланировать', callback_data='admin_alert:schedule'),
        ],
        [InlineKeyboardButton('❌ Отмена', callback_data='admin_panel:main')],
    ])

    if menu_message_id:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=menu_message_id,
                text=(
                    f"📢 *Алерт готов:*\n\n"
                    f"{escape_md(alert_text[:200])}\n\n"
                    f"Когда отправить?"
                ),
                reply_markup=keyboard,
                parse_mode='Markdown',
            )
            return
        except TelegramError:
            pass

    sent = await update.message.reply_text(
        text=(
            f"📢 *Алерт готов:*\n\n"
            f"{escape_md(alert_text[:200])}\n\n"
            f"Когда отправить?"
        ),
        reply_markup=keyboard,
        parse_mode='Markdown',
    )
    context.user_data['menu_message_id'] = sent.message_id


async def _show_alert_event_picker(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    pool,
) -> None:
    chat_id = update.effective_chat.id
    menu_message_id = context.user_data.get('menu_message_id')
    event_ids = context.user_data.get('alert_event_ids', [])
    idx = context.user_data.get('alert_event_index', 0)

    if not event_ids:
        return

    surrogate_id = event_ids[idx]
    row = await pool.fetchrow(f"""
        SELECT surrogate_id, text, date
        FROM {Tables.MESSAGES}
        WHERE surrogate_id = $1
    """, surrogate_id)

    if not row:
        return

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton('🔙', callback_data='admin_alert_pick:prev'),
            InlineKeyboardButton(f'{idx+1}/{len(event_ids)}', callback_data='admin_alert_pick:counter'),
            InlineKeyboardButton('🔜', callback_data='admin_alert_pick:next'),
        ],
        [InlineKeyboardButton('✅ Выбрать это событие', callback_data=f'admin_alert_pick:select:{surrogate_id}')],
        [InlineKeyboardButton('❌ Отмена', callback_data='admin_panel:main')],
    ])

    text = (
        f"📋 *Выберите событие:*\n\n"
        f"📅 {row['date']}\n\n"
        f"{escape_md(row['text'][:300])}"
    )

    if menu_message_id:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=menu_message_id,
                text=text,
                reply_markup=keyboard,
                parse_mode='Markdown',
            )
            return
        except TelegramError:
            pass

    sent = await update.message.reply_text(
        text=text,
        reply_markup=keyboard,
        parse_mode='Markdown',
    )
    context.user_data['menu_message_id'] = sent.message_id


async def handle_admin_alert_pick_nav(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    await query.answer()

    if not await _require_admin(update):
        return

    pool = context.bot_data.get('pool')
    parts = query.data.split(':')
    action = parts[1]

    event_ids = context.user_data.get('alert_event_ids', [])
    idx = context.user_data.get('alert_event_index', 0)

    if action == 'prev' and idx > 0:
        context.user_data['alert_event_index'] = idx - 1
    elif action == 'next' and idx < len(event_ids) - 1:
        context.user_data['alert_event_index'] = idx + 1
    elif action == 'select':
        if len(parts) < 3 or not parts[2].isdigit():
            return
        surrogate_id = int(parts[2])
        row = await pool.fetchrow(f"""
                    SELECT text FROM {Tables.MESSAGES} WHERE surrogate_id = $1
                """, surrogate_id)
        if row:
            context.user_data['alert_text'] = row['text']
            context.user_data['alert_msg_sid'] = surrogate_id
            await _show_alert_time_choice(update, context)
            return

    await _show_alert_event_picker(update, context, pool)


async def handle_calendar(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    await query.answer()

    pool = context.bot_data.get('pool')
    today = date.today()
    context.user_data['calendar_year'] = today.year
    context.user_data['calendar_month'] = today.month

    await _show_calendar(update, context, pool)


async def handle_calendar_nav(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    await query.answer()

    pool = context.bot_data.get('pool')
    parts = query.data.split(':')
    direction = parts[1]

    year = context.user_data.get('calendar_year', date.today().year)
    month = context.user_data.get('calendar_month', date.today().month)

    if direction == 'prev':
        month -= 1
        if month < 1:
            month = 12
            year -= 1
    elif direction == 'next':
        month += 1
        if month > 12:
            month = 1
            year += 1

    context.user_data['calendar_year'] = year
    context.user_data['calendar_month'] = month

    await _show_calendar(update, context, pool)


async def _show_calendar(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    pool,
) -> None:
    chat_id = update.effective_chat.id
    menu_message_id = context.user_data.get('menu_message_id')

    year = context.user_data.get('calendar_year', date.today().year)
    month = context.user_data.get('calendar_month', date.today().month)

    row = await pool.fetchrow(f"""
            SELECT
                array_agg(DISTINCT date) as dates,
                MAX(date) as max_date
            FROM {Tables.MESSAGES}
            WHERE date IS NOT NULL
            AND date >= CURRENT_DATE
            AND hidden_by_admin = FALSE
        """)
    dates_with_events = set(row['dates'] or [])
    max_date = row['max_date'] if row and row['max_date'] else date.today()

    keyboard = _build_calendar(
        year=year,
        month=month,
        dates_with_events=dates_with_events,
        max_date=max_date,
        callback_prefix='calendar_date',
        nav_prefix='calendar_nav',
    )
    keyboard.append([InlineKeyboardButton('🏠 В меню', callback_data='nav:menu')])

    await _edit_contact_window(
        context=context,
        chat_id=chat_id,
        menu_message_id=menu_message_id,
        text=f"📅 *Выберите дату:*\n\n{MONTHS_RU[month]} {year}",
        keyboard=InlineKeyboardMarkup(keyboard),
    )

def _parse_date_input(text: str):
    from datetime import date
    m = re.match(r'^(\d{2})\.(\d{2})\.(\d{4})$', text.strip())
    if not m:
        return None
    try:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None


def _parse_time_input(text: str):
    import re
    m = re.match(r'^(\d{1,2}):(\d{2})$', text.strip())
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return (hour, minute)
    return None

def _build_calendar(
    year: int,
    month: int,
    dates_with_events: set,
    max_date: date,
    callback_prefix: str,
    nav_prefix: str,
) -> list:

    today = date.today()
    cal = calendar.monthcalendar(year, month)

    header = [InlineKeyboardButton(d, callback_data='calendar:ignore')
              for d in ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс']]

    rows = [header]

    for week in cal:
        row = []
        for day in week:
            if day == 0:
                row.append(InlineKeyboardButton(' ', callback_data='calendar:ignore'))
                continue

            current_date = date(year, month, day)

            if current_date < today or current_date > max_date:
                row.append(InlineKeyboardButton('·', callback_data='calendar:ignore'))
            elif current_date in dates_with_events:
                date_str = current_date.isoformat()
                row.append(InlineKeyboardButton(
                    str(day),
                    callback_data=f'{callback_prefix}:{date_str}'
                ))
            else:
                row.append(InlineKeyboardButton('·', callback_data='calendar:ignore'))

        rows.append(row)

    current_month = date.today()
    prev_disabled = (year == current_month.year and month == current_month.month)
    next_disabled = (year == max_date.year and month == max_date.month)

    nav = [
        InlineKeyboardButton(
            '◀️' if not prev_disabled else ' ',
            callback_data=f'{nav_prefix}:prev' if not prev_disabled else 'calendar:ignore'
        ),
        InlineKeyboardButton(
            '▶️' if not next_disabled else ' ',
            callback_data=f'{nav_prefix}:next' if not next_disabled else 'calendar:ignore'
        ),
    ]
    rows.append(nav)

    return rows
