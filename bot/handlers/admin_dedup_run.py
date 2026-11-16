"""Ручной запуск дедупликатора: «⚙️ Админка» → «🧩 Запуск дедупа».

Бот и deduplicator — разные контейнеры. Кнопка кладёт запрос в таблицу dedup_requests, сервис deduplicator забирает его
на ближайшем такте (раз в service.poll_seconds), когда не работают коллекторы, и запускает прогон; итог приходит админу сообщением.
Callback_data: ddr:menu  ddr:run  ddr:force  ddr:force_ok  ddr:cancel
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from loguru import logger
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from bot.handlers.reviews import is_admin_user
from bot.utils.messenger import edit_or_send
from db import dedup_requests
from shared.config import config

TZ = ZoneInfo('Europe/Belgrade')


def _method() -> str:
    return (config.get('deduplication') or {}).get('method', 'legacy')


def _fmt(dt) -> str:
    return dt.astimezone(TZ).strftime('%d.%m %H:%M') if dt else '—'


def build_text(info: dict, method: str) -> str:
    lines = ['🧩 Дедупликатор: ручной запуск', '', f'Режим: {method}']
    last = info.get('last')
    if last:
        lines.append(f"Последний прогон: {_fmt(last['started_at'])} · {last['status']} · дат {last['dates_processed']}/{last['dates_total']} "
                     f"· скрыто {last['hidden']}, возвращено {last['unhidden']}")
        if last['status'] in ('error', 'skipped') and last['error']:
            lines.append(f"  причина: {last['error'][:150]}")
    else:
        lines.append('Прогонов ещё не было')
    if info.get('collectors'):
        lines.append(f"⏳ Работают коллекторы ({info['collectors']}): запуск начнётся после их окончания")
    req = info.get('open')
    if req:
        kind = 'полный пересчёт' if req['force'] else 'изменившиеся даты'
        if req['status'] == 'running':
            lines.append(f"▶️ Идёт ручной запуск ({kind}) с {_fmt(req['started_at'])}. Итог придёт сообщением.")
        else:
            lines.append(f"🕓 Запрос в очереди ({kind}) с {_fmt(req['requested_at'])}: сервис подхватит его в течение минуты, "
                         f"если коллекторы не работают.")
    else:
        lr = info.get('last_request')
        if lr:
            lines.append(f"Прошлый ручной запуск: {_fmt(lr['requested_at'])} · {'готово' if lr['status'] == 'done' else 'ошибка'}"
                         + (f" ({lr['error'][:100]})" if lr['error'] else ''))
    return '\n'.join(lines)


def build_keyboard(info: dict, method: str) -> InlineKeyboardMarkup:
    rows = []
    if info.get('open'):
        if info['open']['status'] == 'pending':
            rows.append([InlineKeyboardButton('❌ Отменить запрос', callback_data='ddr:cancel')])
        rows.append([InlineKeyboardButton('🔄 Обновить', callback_data='ddr:menu')])
    elif method in ('shadow', 'ai'):
        rows.append([InlineKeyboardButton('▶️ Запустить (изменившиеся даты)', callback_data='ddr:run')])
        rows.append([InlineKeyboardButton('🔁 Полный пересчёт всех дат', callback_data='ddr:force')])
    rows.append([InlineKeyboardButton('◀️ В админку', callback_data='admin_panel:main')])
    return InlineKeyboardMarkup(rows)


async def _show_menu(update, context, note: str = '') -> None:
    pool = context.bot_data.get('pool')
    method = _method()
    info = await dedup_requests.status_info(pool)
    text = build_text(info, method)
    if method not in ('shadow', 'ai'):
        text += '\n\n⛔ Ручной запуск доступен только при deduplication.method = shadow или ai.'
    if note:
        text = f'{note}\n\n{text}'
    await edit_or_send(context=context, chat_id=update.effective_chat.id, text=text,
                       keyboard=build_keyboard(info, method), parse_mode=None)


async def handle_dedup_run(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not is_admin_user(update.effective_user.id if update.effective_user else None):
        await query.answer('Недоступно', show_alert=True)
        return
    action = (query.data or '').split(':')[1] if ':' in (query.data or '') else ''
    pool = context.bot_data.get('pool')

    if action == 'menu':
        await query.answer()
        await _show_menu(update, context)
        return

    if action == 'force':
        await query.answer()
        await edit_or_send(
            context=context, chat_id=update.effective_chat.id, parse_mode=None,
            text=('🔁 Полный пересчёт всех дат?\n\n'
                  'ИИ заново разберёт каждую будущую дату, игнорируя сохранённые отпечатки. Это сотни запросов к модели '
                  'и заметный расход токенов. Нужен после смены priority, моделей или промпта. Прежние пары сохраняются '
                  '(«липкие»), решения админа не пересчитываются.'),
            keyboard=InlineKeyboardMarkup([
                [InlineKeyboardButton('✅ Запустить', callback_data='ddr:force_ok'),
                 InlineKeyboardButton('❌ Отмена', callback_data='ddr:menu')]]))
        return

    if action == 'cancel':
        ok = await dedup_requests.cancel_pending(pool)
        await query.answer('Запрос отменён' if ok else 'Запрос уже взят в работу или отсутствует')
        await _show_menu(update, context)
        return

    if action in ('run', 'force_ok'):
        method = _method()
        if method not in ('shadow', 'ai'):
            await query.answer('Режим дедупа не shadow/ai', show_alert=True)
            return
        force = action == 'force_ok'
        try:
            req_id = await dedup_requests.create(pool, update.effective_user.id, force)
        except Exception as e:
            logger.error(f"DEDUP_REQUEST_CREATE_FAILED | {type(e).__name__}: {e}")
            await query.answer('Не удалось создать запрос', show_alert=True)
            return
        if req_id is None:
            await query.answer('Уже есть открытый запрос', show_alert=True)
            await _show_menu(update, context)
            return
        logger.info(f"DEDUP_REQUEST | id={req_id} | force={force} | by={update.effective_user.id}")
        await query.answer('Запрос принят')
        await _show_menu(update, context, note='✅ Запрос принят.')
        return

    await query.answer('Неверный запрос', show_alert=True)
