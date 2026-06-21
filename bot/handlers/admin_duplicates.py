from difflib import SequenceMatcher
from loguru import logger
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from db.tables import Tables
from shared.constants import (
    DUPLICATE_REVIEW_PREVIEW_LENGTH,
    DUPLICATE_QUICK_COMPARE_LENGTH,
)
from bot.handlers.contact import _require_admin, _edit_contact_window


async def handle_admin_duplicates(
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

    rows = await pool.fetch(f"""
        SELECT surrogate_id, text, date, channel_id, duplicate_of
        FROM {Tables.MESSAGES}
        WHERE hidden_by_admin = TRUE
        ORDER BY date, created_at
        LIMIT 10
    """)

    if not rows:
        await _edit_contact_window(
            context=context,
            chat_id=chat_id,
            menu_message_id=menu_message_id,
            text="✅ *Помеченных дублей нет.*",
            keyboard=InlineKeyboardMarkup([[
                InlineKeyboardButton('🏠 В меню', callback_data='nav:menu')
            ]]),
        )
        return

    context.user_data['duplicate_ids'] = [r['surrogate_id'] for r in rows]
    context.user_data['duplicate_index'] = 0

    await show_duplicate(update, context, pool)


async def show_duplicate(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    pool,
) -> None:
    from bot.handlers.contact import escape_md

    chat_id = update.effective_chat.id
    menu_message_id = context.user_data.get('menu_message_id')
    duplicate_ids = context.user_data.get('duplicate_ids', [])
    idx = context.user_data.get('duplicate_index', 0)

    flagged = None
    while idx < len(duplicate_ids):
        surrogate_id = duplicate_ids[idx]
        flagged = await pool.fetchrow(f"""
            SELECT surrogate_id, text, date, channel_id, duplicate_of
            FROM {Tables.MESSAGES}
            WHERE surrogate_id = $1
        """, surrogate_id)

        if flagged:
            break

        idx += 1
        context.user_data['duplicate_index'] = idx

    if idx >= len(duplicate_ids) or not flagged:
        await _edit_contact_window(
            context=context,
            chat_id=chat_id,
            menu_message_id=menu_message_id,
            text="✅ *Все дубли обработаны.*",
            keyboard=InlineKeyboardMarkup([[
                InlineKeyboardButton('🏠 В меню', callback_data='nav:menu')
            ]]),
        )
        return

    surrogate_id = duplicate_ids[idx]

    existing = None
    ratio = 0.0

    if flagged['duplicate_of']:
        existing = await pool.fetchrow(f"""
            SELECT surrogate_id, text, channel_id
            FROM {Tables.MESSAGES}
            WHERE surrogate_id = $1
        """, flagged['duplicate_of'])
        if existing:
            ratio = round(
                SequenceMatcher(None, flagged['text'], existing['text']).ratio(), 2
            )

    if not existing and flagged['date']:
        rows = await pool.fetch(f"""
            SELECT surrogate_id, text, channel_id
            FROM {Tables.MESSAGES}
            WHERE date = $1
            AND channel_id != $2
            AND hidden_by_admin = FALSE
            AND surrogate_id != $3
        """, flagged['date'], flagged['channel_id'], surrogate_id)

        best_ratio = 0.0
        best_row = None
        for row in rows:
            quick_ratio = SequenceMatcher(
                None,
                flagged['text'][:DUPLICATE_QUICK_COMPARE_LENGTH],
                row['text'][:DUPLICATE_QUICK_COMPARE_LENGTH],
            ).ratio()
            if quick_ratio < 0.3:
                continue
            r = SequenceMatcher(None, flagged['text'], row['text']).ratio()
            if r > best_ratio:
                best_ratio = r
                best_row = dict(row)
        existing = best_row
        ratio = round(best_ratio, 2)

    flagged_text = escape_md(flagged['text'][:DUPLICATE_REVIEW_PREVIEW_LENGTH])
    existing_text = escape_md(existing['text'][:DUPLICATE_REVIEW_PREVIEW_LENGTH]) if existing else ''

    text = (
        f"🔍 *Возможный дубль* (схожесть: {ratio*100:.0f}%)\n"
        f"_{idx+1} из {len(duplicate_ids)}_\n\n"
        f"*НОВОЕ* (id={surrogate_id}):\n"
        f"{flagged_text}\n\n"
    )

    if existing:
        text += (
            f"*СУЩЕСТВУЮЩЕЕ* (id={existing['surrogate_id']}):\n"
            f"{existing_text}"
        )

    existing_sid = existing['surrogate_id'] if existing else None

    buttons = [
        [
            InlineKeyboardButton('✅ Оставить новое', callback_data=f'dedup:keep_new:{surrogate_id}'),
            InlineKeyboardButton('🗑 Скрыть новое', callback_data=f'dedup:hide_new:{surrogate_id}'),
        ],
    ]

    if existing_sid:
        buttons.append([
            InlineKeyboardButton(
                '🗑 Скрыть старое',
                callback_data=f'dedup:hide_old:{existing_sid}:{surrogate_id}'
            ),
            InlineKeyboardButton(
                '✅ Оставить оба',
                callback_data=f'dedup:keep_both:{surrogate_id}'
            ),
        ])
    else:
        buttons.append([
            InlineKeyboardButton(
                '✅ Оставить оба',
                callback_data=f'dedup:keep_both:{surrogate_id}'
            ),
        ])

    buttons.append([InlineKeyboardButton('◀️ В меню', callback_data='nav:menu')])

    await _edit_contact_window(
        context=context,
        chat_id=chat_id,
        menu_message_id=menu_message_id,
        text=text,
        keyboard=InlineKeyboardMarkup(buttons),
    )


async def handle_dedup_action(
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

    if action == 'keep_new':
        if len(parts) < 3 or not parts[2].isdigit():
            return
        surrogate_id = int(parts[2])
        await pool.execute(
            f"UPDATE {Tables.MESSAGES} SET hidden_by_admin = FALSE, duplicate_of = NULL WHERE surrogate_id = $1",
            surrogate_id
        )
        logger.info(f"DEDUP_KEEP_NEW | surrogate_id={surrogate_id}")

    elif action == 'hide_new':
        if len(parts) < 3 or not parts[2].isdigit():
            return
        surrogate_id = int(parts[2])
        await pool.execute(
            f"UPDATE {Tables.MESSAGES} SET hidden_by_admin = TRUE, duplicate_of = NULL WHERE surrogate_id = $1",
            surrogate_id
        )
        logger.info(f"DEDUP_HIDE_NEW | surrogate_id={surrogate_id}")

    elif action == 'hide_old':
        if len(parts) < 4 or not parts[2].isdigit() or not parts[3].isdigit():
            return
        old_sid = int(parts[2])
        new_sid = int(parts[3])
        await pool.execute(
            f"UPDATE {Tables.MESSAGES} SET hidden_by_admin = TRUE, duplicate_of = NULL WHERE surrogate_id = $1",
            old_sid
        )
        await pool.execute(
            f"UPDATE {Tables.MESSAGES} SET hidden_by_admin = FALSE, duplicate_of = NULL WHERE surrogate_id = $1",
            new_sid
        )
        logger.info(f"DEDUP_HIDE_OLD | old_sid={old_sid} | new_sid={new_sid}")

    elif action == 'keep_both':
        if len(parts) < 3 or not parts[2].isdigit():
            return
        surrogate_id = int(parts[2])
        await pool.execute(
            f"UPDATE {Tables.MESSAGES} SET hidden_by_admin = FALSE, duplicate_of = NULL WHERE surrogate_id = $1",
            surrogate_id
        )
        logger.info(f"DEDUP_KEEP_BOTH | surrogate_id={surrogate_id}")

    context.user_data['duplicate_index'] = context.user_data.get('duplicate_index', 0) + 1
    await show_duplicate(update, context, pool)
