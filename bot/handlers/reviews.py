"""Пересмотр событий (только админ): кнопка на карточке → причина → запись в event_reviews.

Поток: «🔎 Пересмотр» → окно с текстом события и кнопками «Неправильная дата» / «Неправильная категория» /
«Ввести вручную» / «Назад». Дата и ручной текст вводятся сообщением; категория выбирается кнопкой (есть «Другое» —
только здесь). Сохраняется снимок записи, причина и исправление; в events / messages ничего не меняется.
Пока пересмотр открыт (status = 'open'), кнопка на карточке неактивна и называется «На пересмотре». Закрывается в БД.
Callback_data: rv:open:<ref> rv:date:<ref> rv:cat:<ref> rv:man:<ref> rv:setcat:<ref>:<code> rv:busy
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime

from loguru import logger
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from bot.formatters.event import build_entities
from bot.formatters.event_card import build_event_card
from bot.utils.categories import CATEGORIES
from bot.utils.messenger import edit_or_send
from bot.utils.refs import is_event_ref, parse_ref, ref_id
from collector.entities.truncator import EventText, truncate_if_needed
from db.tables import Tables
from shared.config import config

OTHER_CODE = 'other'
OTHER_LABEL = 'Другое'
MANUAL_MAX_CHARS = 2000
POST_VIEW_LIMIT = 3800          # запас до 4096 символов сообщения
_DATE_RE = re.compile(r'^(\d{1,2})\.(\d{1,2})\.(\d{4})$')

REASON_DATE = 'wrong_date'
REASON_CATEGORY = 'wrong_category'
REASON_MANUAL = 'manual'


def is_admin_user(user_id: int | None) -> bool:
    return bool(user_id) and user_id == config['telegram']['admin_user_id']


def parse_review_date(text: str) -> date | None:
    """'25.12.2026' → date; мусор и несуществующие даты → None."""
    m = _DATE_RE.match((text or '').strip())
    if not m:
        return None
    try:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None


def category_label(code: str) -> str | None:
    """Название категории по коду кнопки; 'other' → «Другое»; неизвестный код → None."""
    if code == OTHER_CODE:
        return OTHER_LABEL
    for c in CATEGORIES:
        if c.code == code:
            return c.label
    return None


# ---------- клавиатуры ----------

def build_review_keyboard(ref) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('🔎 Что не так с событием?', callback_data='rv:noop')],
        [
            InlineKeyboardButton('📅 Неправильная дата', callback_data=f'rv:date:{ref}'),
            InlineKeyboardButton('🗂 Неправильная категория', callback_data=f'rv:cat:{ref}'),
        ],
        [InlineKeyboardButton('✍️ Ввести вручную', callback_data=f'rv:man:{ref}')],
        [InlineKeyboardButton('⬅️ Назад', callback_data='rv:back')],
    ])


def build_category_keyboard(ref) -> InlineKeyboardMarkup:
    rows, pair = [], []
    for c in CATEGORIES:
        pair.append(InlineKeyboardButton(f'{c.emoji} {c.label}', callback_data=f'rv:setcat:{ref}:{c.code}'))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([InlineKeyboardButton(f'❔ {OTHER_LABEL}', callback_data=f'rv:setcat:{ref}:{OTHER_CODE}')])
    rows.append([InlineKeyboardButton('⬅️ Назад', callback_data=f'rv:open:{ref}')])
    return InlineKeyboardMarkup(rows)


def build_input_keyboard(ref) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton('⬅️ Назад', callback_data=f'rv:open:{ref}')]])


# ---------- БД ----------

def _where(ref) -> tuple[str, str]:
    """(колонка в event_reviews, имя для лога)."""
    return ('event_id', 'event') if is_event_ref(ref) else ('message_id', 'post')


async def is_under_review(pool, ref) -> bool:
    """Есть ли открытый пересмотр. Сбой БД не должен ломать карточку — тогда считаем, что нет."""
    column, _ = _where(ref)
    try:
        return bool(await pool.fetchval(
            f"SELECT 1 FROM {Tables.EVENT_REVIEWS} WHERE {column} = $1 AND status = 'open' LIMIT 1", ref_id(ref)))
    except Exception as e:
        logger.warning(f"REVIEW_STATE_FAILED | ref={ref} | {type(e).__name__}: {e}")
        return False


async def _load_record(pool, ref) -> dict | None:
    if is_event_ref(ref):
        row = await pool.fetchrow(f"SELECT * FROM {Tables.EVENTS} WHERE id = $1", ref_id(ref))
    else:
        row = await pool.fetchrow(
            f"SELECT m.*, c.username AS channel_username, c.title AS channel_title "
            f"FROM {Tables.MESSAGES} m LEFT JOIN {Tables.CHANNELS} c ON c.channel_id = m.channel_id "
            f"WHERE m.surrogate_id = $1", ref_id(ref))
    return dict(row) if row else None


def build_review_values(ref, rec: dict) -> dict:
    """Поля записи пересмотра из строки events / messages."""
    if is_event_ref(ref):
        return {
            'kind': 'event', 'event_id': ref_id(ref), 'message_id': None,
            'title': rec.get('title'), 'description': rec.get('description'),
            'starts_at': rec.get('starts_at'), 'venue': rec.get('venue'),
            'source': rec.get('source'), 'source_url': rec.get('source_url'),
            'event_types': list(rec.get('event_types') or []),
        }
    text = rec.get('text') or ''
    username = rec.get('channel_username')
    first_line = next((ln.strip() for ln in text.splitlines() if ln.strip()), '')
    return {
        'kind': 'post', 'event_id': None, 'message_id': ref_id(ref),
        'title': first_line[:120], 'description': text,
        'starts_at': rec.get('date'), 'venue': None,
        'source': (f'@{username}' if username else rec.get('channel_title') or str(rec.get('channel_id'))),
        'source_url': f'https://t.me/{username}/{rec.get("id")}' if username and rec.get('id') else None,
        'event_types': list(rec.get('event_types') or []),
    }


def _json_default(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


async def save_review(pool, ref, reason_code: str, reason_text: str | None = None,
                      correct_category: str | None = None, correct_date: date | None = None) -> str:
    """Записывает пересмотр. 'created' — записан; 'exists' — уже открыт; 'missing' — записи нет."""
    rec = await _load_record(pool, ref)
    if rec is None:
        return 'missing'
    v = build_review_values(ref, rec)
    snapshot = json.dumps(rec, default=_json_default, ensure_ascii=False)
    new_id = await pool.fetchval(
        f"""
        INSERT INTO {Tables.EVENT_REVIEWS}
            (kind, event_id, message_id, reason_code, reason_text, correct_category, correct_date,
             title, description, starts_at, venue, source, source_url, event_types, snapshot)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15::jsonb)
        ON CONFLICT DO NOTHING
        RETURNING id
        """,
        v['kind'], v['event_id'], v['message_id'], reason_code, reason_text, correct_category, correct_date,
        v['title'], v['description'], v['starts_at'], v['venue'], v['source'], v['source_url'],
        v['event_types'], snapshot,
    )
    if new_id is None:
        return 'exists'
    logger.info(f"REVIEW_SAVED | id={new_id} | ref={ref} | reason={reason_code} | "
                f"category={correct_category!r} | date={correct_date}")
    return 'created'


# ---------- окна ----------

async def _show_review_window(update: Update, context: ContextTypes.DEFAULT_TYPE, ref) -> None:
    """Текст события (как на карточке, целиком) и кнопки причин."""
    from bot.handlers.events import fetch_event_by_id

    pool = context.bot_data.get('pool')
    event = await fetch_event_by_id(pool, ref, update.effective_user.id)
    if not event:
        await update.callback_query.answer('Запись не найдена', show_alert=True)
        return

    if event['kind'] == 'e':
        card = build_event_card(event, expanded=True)
        text, entities = card.text, card.entities
    else:
        truncated = truncate_if_needed(
            EventText(text=event['text'], entities=event['entities'] or []), limit=POST_VIEW_LIMIT)
        text, entities = truncated.text, build_entities(truncated.text, truncated.entities)

    await edit_or_send(
        context=context, chat_id=update.effective_chat.id, text=text,
        keyboard=build_review_keyboard(ref), entities=entities, disable_link_preview=True,
    )


async def _back_to_card(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from bot.handlers.events import show_current_event
    await show_current_event(update, context, context.bot_data.get('pool'))


async def _prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, ref, mode: str, text: str) -> None:
    context.user_data['review_input'] = {'mode': mode, 'ref': ref}
    await edit_or_send(
        context=context, chat_id=update.effective_chat.id, text=text,
        keyboard=build_input_keyboard(ref), parse_mode=None,
    )


# ---------- обработчики ----------

async def handle_review(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Все кнопки rv:*."""
    query = update.callback_query
    if not is_admin_user(update.effective_user.id if update.effective_user else None):
        await query.answer('Недоступно', show_alert=True)
        return

    parts = query.data.split(':')
    action = parts[1] if len(parts) > 1 else ''

    if action == 'noop':
        await query.answer()
        return
    if action == 'busy':
        await query.answer('Это событие уже на пересмотре', show_alert=False)
        return

    if action == 'back':
        await query.answer()
        context.user_data.pop('review_input', None)
        await _back_to_card(update, context)
        return

    ref = parse_ref(parts[2]) if len(parts) > 2 else None
    if ref is None:
        await query.answer('Неверный запрос', show_alert=True)
        return

    pool = context.bot_data.get('pool')
    if action != 'setcat' and await is_under_review(pool, ref) and action != 'open':
        await query.answer('Это событие уже на пересмотре', show_alert=True)
        return

    if action == 'open':
        if await is_under_review(pool, ref):
            await query.answer('Это событие уже на пересмотре', show_alert=True)
            await _back_to_card(update, context)
            return
        await query.answer()
        context.user_data.pop('review_input', None)
        await _show_review_window(update, context, ref)
        return

    if action == 'date':
        await query.answer()
        await _prompt(update, context, ref, 'date',
                      '📅 Введите правильную дату события сообщением в формате ДД.ММ.ГГГГ\n(например, 25.12.2026).')
        return

    if action == 'man':
        await query.answer()
        await _prompt(update, context, ref, 'manual',
                      f'✍️ Опишите, что не так с событием, одним сообщением (до {MANUAL_MAX_CHARS} символов).')
        return

    if action == 'cat':
        await query.answer()
        context.user_data.pop('review_input', None)
        await edit_or_send(
            context=context, chat_id=update.effective_chat.id, text='🗂 Выберите правильную категорию:',
            keyboard=build_category_keyboard(ref), parse_mode=None,
        )
        return

    if action == 'setcat':
        label = category_label(parts[3]) if len(parts) > 3 else None
        if label is None:
            await query.answer('Неизвестная категория', show_alert=True)
            return
        await query.answer()
        await _finish(update, context, ref, REASON_CATEGORY, correct_category=label)
        return

    await query.answer('Неверный запрос', show_alert=True)


async def _finish(update: Update, context: ContextTypes.DEFAULT_TYPE, ref, reason_code: str, **fields) -> None:
    pool = context.bot_data.get('pool')
    context.user_data.pop('review_input', None)
    try:
        result = await save_review(pool, ref, reason_code, **fields)
    except Exception as e:
        logger.error(f"REVIEW_SAVE_FAILED | ref={ref} | {type(e).__name__}: {e}")
        if update.callback_query:
            await update.callback_query.answer('Не удалось сохранить пересмотр', show_alert=True)
        else:
            await update.message.reply_text('❌ Не удалось сохранить пересмотр. Попробуйте ещё раз.')
        return
    if result == 'missing':
        text = 'Запись не найдена'
        if update.callback_query:
            await update.callback_query.answer(text, show_alert=True)
        else:
            await update.message.reply_text(f'❌ {text}')
        return
    await _back_to_card(update, context)


async def handle_review_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Текст админа, пока открыт ввод даты / ручной причины. True — сообщение обработано."""
    state = context.user_data.get('review_input')
    if not state or not is_admin_user(update.effective_user.id if update.effective_user else None):
        return False

    text = (update.message.text or '').strip()
    ref, mode = state['ref'], state['mode']

    if mode == 'date':
        day = parse_review_date(text)
        if day is None:
            await update.message.reply_text('❌ Неверная дата. Введите в формате ДД.ММ.ГГГГ или нажмите «Назад».')
            return True
        reason, fields = REASON_DATE, {'correct_date': day}
    else:
        if not text:
            return True
        if len(text) > MANUAL_MAX_CHARS:
            await update.message.reply_text(
                f'❌ Слишком длинно ({len(text)} символов). Сократите до {MANUAL_MAX_CHARS} и отправьте снова.')
            return True
        reason, fields = REASON_MANUAL, {'reason_text': text}

    try:
        await update.message.delete()
    except Exception:
        pass
    await _finish(update, context, ref, reason, **fields)
    return True
