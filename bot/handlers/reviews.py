"""Пересмотр событий (только админ): кнопка на карточке → причина → запись в event_reviews.

Поток: «🔎 Пересмотр» → окно с текстом события и кнопками «Неправильная дата» / «Неправильная категория» /
«Ввести вручную» / «Назад». Дата и ручной текст вводятся сообщением — только запись в event_reviews.
Категория выбирается кнопкой: реальная категория заменяет event_types целиком (events — с category_manual) и
пишется в event_reviews со статусом 'applied' («Пересмотрено»); «Другое» — только запись (status 'open').
Пока пересмотр открыт — кнопка на карточке «На пересмотре», после применения категории — «Пересмотрено»; обе неактивны.
Callback_data: rv:open:<ref> rv:date:<ref> rv:cat:<ref> rv:man:<ref> rv:setcat:<ref>:<code> rv:busy
"""

from __future__ import annotations

import re
from datetime import date

from loguru import logger
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from bot.formatters.event import build_entities
from bot.formatters.event_card import build_event_card
from bot.utils.categories import CATEGORIES
from bot.utils import moderation
from bot.utils.messenger import edit_or_send
from bot.utils.moderation import build_review_values  # noqa: F401  (обратная совместимость)
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


async def review_state(pool, ref) -> str | None:
    """'open' — на пересмотре, 'applied' — пересмотрено, None — нет. Сбой БД не ломает карточку."""
    column, _ = _where(ref)
    try:
        states = await pool.fetch(
            f"SELECT DISTINCT status FROM {Tables.EVENT_REVIEWS} "
            f"WHERE flow = 'review' AND {column} = $1 AND status IN ('open', 'applied')", ref_id(ref))
    except Exception as e:
        logger.warning(f"REVIEW_STATE_FAILED | ref={ref} | {type(e).__name__}: {e}")
        return None
    found = {r['status'] for r in states}
    if 'open' in found:
        return 'open'
    return 'applied' if 'applied' in found else None


async def is_under_review(pool, ref) -> bool:
    return await review_state(pool, ref) == 'open'


async def _load_record(pool, ref) -> dict | None:
    return await moderation.load_record(pool, ref)


async def save_review(pool, ref, reason_code: str, reason_text: str | None = None,
                      correct_category: str | None = None, correct_date: date | None = None) -> str:
    """Записывает пересмотр. 'created' — записан; 'exists' — уже открыт; 'missing' — записи нет.

    Реальная категория (кроме «Другое») дополнительно записывается в events / messages, статус 'applied'.
    Дата и ручной ввод — только запись в event_reviews.
    """
    applied = reason_code == REASON_CATEGORY and correct_category not in (None, OTHER_LABEL)
    if applied:
        result = await moderation.decide(
            pool, ref, flow=moderation.FLOW_REVIEW, reason_code=REASON_CATEGORY,
            status=moderation.STATUS_APPLIED, correct_category=correct_category)
        return 'created' if result == 'done' else 'missing'
    rec = await moderation.load_record(pool, ref)
    if rec is None:
        return 'missing'
    new_id = await moderation.insert_decision(
        pool, ref, rec, flow=moderation.FLOW_REVIEW, reason_code=reason_code, status=moderation.STATUS_OPEN,
        reason_text=reason_text, correct_category=correct_category, correct_date=correct_date)
    if new_id is None:
        return 'exists'
    logger.info(f"REVIEW_SAVED | id={new_id} | ref={ref} | reason={reason_code} | "
                f"category={correct_category!r} | date={correct_date}")
    return 'created'


# ---------- окна ----------

def render_full_view(event: dict) -> tuple[str, list]:
    """Полный текст события / поста (как на карточке) и entities — для окон админа."""
    if event['kind'] == 'e':
        card = build_event_card(event, expanded=True)
        return card.text, card.entities
    truncated = truncate_if_needed(
        EventText(text=event['text'], entities=event['entities'] or []), limit=POST_VIEW_LIMIT)
    return truncated.text, build_entities(truncated.text, truncated.entities)


async def _show_review_window(update: Update, context: ContextTypes.DEFAULT_TYPE, ref) -> None:
    """Текст события (как на карточке, целиком) и кнопки причин."""
    from bot.handlers.events import fetch_event_by_id

    pool = context.bot_data.get('pool')
    event = await fetch_event_by_id(pool, ref, update.effective_user.id)
    if not event:
        await update.callback_query.answer('Запись не найдена', show_alert=True)
        return

    text, entities = render_full_view(event)

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
        await query.answer('Это событие уже на пересмотре или пересмотрено', show_alert=False)
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
    if action != 'open' and await review_state(pool, ref):
        await query.answer('Это событие уже на пересмотре или пересмотрено', show_alert=True)
        return

    if action == 'open':
        if await review_state(pool, ref):
            await query.answer('Это событие уже на пересмотре или пересмотрено', show_alert=True)
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
