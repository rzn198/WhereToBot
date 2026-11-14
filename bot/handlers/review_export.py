"""Выгрузка пересмотров админу файлом CSV: «⚙️ Админка» → «📤 Выгрузка пересмотров».

Callback_data: rvx:menu  rvx:get:<open|all|nd|nc>
"""
from __future__ import annotations

import io
from datetime import date

from loguru import logger
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from bot.handlers.reviews import is_admin_user
from bot.utils.messenger import edit_or_send
from db import review_export


def build_menu_keyboard() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(label, callback_data=f'rvx:get:{code}')]
            for code, (_, _, label) in review_export.SCOPES.items()]
    rows.append([InlineKeyboardButton('◀️ В админку', callback_data='admin_panel:main')])
    return InlineKeyboardMarkup(rows)


async def handle_review_export(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not is_admin_user(update.effective_user.id if update.effective_user else None):
        await query.answer('Недоступно', show_alert=True)
        return
    parts = query.data.split(':')
    action = parts[1] if len(parts) > 1 else ''

    if action == 'menu':
        await query.answer()
        await edit_or_send(
            context=context, chat_id=update.effective_chat.id,
            text='📤 Выгрузка пересмотров (CSV): что выгрузить?',
            keyboard=build_menu_keyboard(), parse_mode=None,
        )
        return

    scope = parts[2] if action == 'get' and len(parts) > 2 else None
    if scope not in review_export.SCOPES:
        await query.answer('Неверный запрос', show_alert=True)
        return

    pool = context.bot_data.get('pool')
    try:
        rows = await review_export.fetch_rows(pool, scope)
    except Exception as e:
        logger.error(f"REVIEW_EXPORT_FAILED | scope={scope} | {type(e).__name__}: {e}")
        await query.answer('Не удалось выгрузить', show_alert=True)
        return
    if not rows:
        await query.answer('Записей нет', show_alert=True)
        return

    await query.answer()
    _, part, label = review_export.SCOPES[scope]
    name = f"event_reviews_{part}_{date.today():%Y%m%d}.csv"
    await context.bot.send_document(
        chat_id=update.effective_chat.id, document=io.BytesIO(review_export.to_csv_bytes(rows)), filename=name,
        caption=f"{label}: {len(rows)} шт.",
    )
    logger.info(f"REVIEW_EXPORT | scope={scope} | rows={len(rows)}")
