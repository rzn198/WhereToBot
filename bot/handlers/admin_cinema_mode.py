"""Админка → «🎬 Режим киноафиши»: какая афиша открывается по кнопке «Афиша кинотеатров».

old — прежняя афиша; new_admin — новая (окно со списком фильмов) только админу, остальным прежняя; new — новая всем.
Значение хранится в БД (bot_settings, ключ ui.cinema_mode) и главнее config.yaml (ui.cinema_mode); «Сбросить к конфигу»
удаляет значение из БД. Включение для всех (new) требует подтверждения.
Callback_data: cim:menu  cim:set:<mode>  cim:ok:<mode>  cim:reset
"""
from __future__ import annotations

from loguru import logger
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from bot.handlers.reviews import is_admin_user
from bot.utils.messenger import edit_or_send
from db import settings
from shared.tz import BELGRADE_TZ

MODE_TITLES = {
    'old': '📇 old — прежняя афиша',
    'new_admin': '🧪 new_admin — новая только админу',
    'new': '🎬 new — новая всем',
}
BACK = [InlineKeyboardButton('◀️ В админку', callback_data='admin_panel:main')]


def _fmt(dt) -> str:
    return dt.astimezone(BELGRADE_TZ).strftime('%d.%m %H:%M') if dt else '—'


def build_text(current: str, row, config_mode: str, note: str = '') -> str:
    lines = ['🎬 Режим киноафиши', '']
    if note:
        lines += [note, '']
    if row is None:
        lines.append(f'Сейчас: {current} (из config.yaml)')
    else:
        lines.append(f"Сейчас: {current} (из БД, изменён {_fmt(row['updated_at'])}, админ {row['updated_by'] or '—'})")
    lines.append(f'По умолчанию из config.yaml: {config_mode}')
    return '\n'.join(lines)


def build_keyboard(current: str, has_db_value: bool) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(('✅ ' if m == current else '') + title, callback_data=f'cim:set:{m}')]
            for m, title in MODE_TITLES.items()]
    if has_db_value:
        rows.append([InlineKeyboardButton('↩️ Сбросить к конфигу', callback_data='cim:reset')])
    rows.append(BACK)
    return InlineKeyboardMarkup(rows)


async def _show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, note: str = '') -> None:
    pool = context.bot_data.get('pool')
    try:
        row = await settings.get_setting_row(pool, settings.KEY_CINEMA_MODE)
    except Exception as e:
        logger.error(f'CINEMA_MODE | не прочитать настройку: {type(e).__name__}: {e}')
        await edit_or_send(context=context, chat_id=update.effective_chat.id,
                           text='⚠️ Не удалось прочитать настройку из БД. Бот показывает прежнюю афишу.',
                           keyboard=InlineKeyboardMarkup([BACK]), parse_mode=None)
        return
    current = settings.normalize_cinema_mode(row['value']) if row else settings.config_cinema_mode()
    shown = current or settings.DEFAULT_CINEMA_MODE
    if row and current is None:
        note = (note + '\n' if note else '') + f"⚠️ В БД повреждённое значение {row['value']!r}: бот показывает прежнюю афишу."
    await edit_or_send(
        context=context, chat_id=update.effective_chat.id,
        text=build_text(shown, row, settings.config_cinema_mode(), note),
        keyboard=build_keyboard(shown, row is not None), parse_mode=None)


async def _apply(update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str) -> None:
    admin_id = update.effective_user.id
    try:
        await settings.set_setting(context.bot_data.get('pool'), settings.KEY_CINEMA_MODE, mode, admin_id)
    except Exception as e:
        logger.error(f'CINEMA_MODE | не записать {mode}: {type(e).__name__}: {e}')
        await update.callback_query.answer('Не удалось записать в БД', show_alert=True)
        return
    logger.info(f'CINEMA_MODE | set | admin_id={admin_id} | mode={mode}')
    await _show_menu(update, context, note=f'✅ Режим изменён: {mode}')


async def handle_cinema_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not is_admin_user(update.effective_user.id):
        await query.answer('Нет доступа', show_alert=True)
        return
    parts = query.data.split(':')
    action = parts[1] if len(parts) > 1 else ''
    mode = parts[2] if len(parts) > 2 else ''

    if action == 'menu':
        await query.answer()
        await _show_menu(update, context)
    elif action == 'set' and mode in settings.CINEMA_MODES:
        await query.answer()
        if mode == 'new':
            await edit_or_send(
                context=context, chat_id=update.effective_chat.id,
                text='⚠️ Включить новую киноафишу для ВСЕХ пользователей?\n\nОткатить можно здесь же, выбрав old.',
                keyboard=InlineKeyboardMarkup([[InlineKeyboardButton('✅ Включить', callback_data='cim:ok:new'),
                                                InlineKeyboardButton('❌ Отмена', callback_data='cim:menu')]]), parse_mode=None)
        else:
            await _apply(update, context, mode)
    elif action == 'ok' and mode in settings.CINEMA_MODES:
        await query.answer()
        await _apply(update, context, mode)
    elif action == 'reset':
        await query.answer()
        try:
            await settings.delete_setting(context.bot_data.get('pool'), settings.KEY_CINEMA_MODE)
        except Exception as e:
            logger.error(f'CINEMA_MODE | не сбросить: {type(e).__name__}: {e}')
            await query.answer('Не удалось сбросить', show_alert=True)
            return
        logger.info(f'CINEMA_MODE | reset | admin_id={update.effective_user.id}')
        await _show_menu(update, context, note='✅ Сброшено к config.yaml')
    else:
        await query.answer('Неверный запрос', show_alert=True)
