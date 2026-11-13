"""«🧩 Неразобранные» (только админ): очереди записей без даты и без категории.

Меню → «Без даты» / «Без категории» → карточка записи с кнопками решения. Очереди общие, решения вечные:
показываются только записи, по которым в event_reviews ещё нет решения этой очереди (flow = 'no_date' / 'no_category').

Callback_data: un:menu  un:noop  un:q:<nd|nc>  un:show  un:date:<ref>  un:cat:<ref>
               un:set:<ref>:<code>  un:rej:<ref>  un:skip:<ref>  un:hide:<ref>
Состояние: user_data['un'] = {'flow', 'ids', 'i'};  user_data['un_input'] = {'ref'} (ожидается ввод даты).
"""

from __future__ import annotations

from loguru import logger
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from bot.handlers.events import EVENTS_CITY, fetch_event_by_id
from bot.handlers.reviews import is_admin_user, parse_review_date, render_full_view
from bot.utils import moderation
from bot.utils.categories import CATEGORIES
from bot.utils.messenger import edit_or_send
from bot.utils.refs import parse_ref

QUEUE_CODES = {'nd': moderation.FLOW_NO_DATE, 'nc': moderation.FLOW_NO_CATEGORY}
QUEUE_TITLES = {moderation.FLOW_NO_DATE: 'Без даты', moderation.FLOW_NO_CATEGORY: 'Без категории'}
STALE = 'Запись уже обработана или устарела'


def _state(context) -> dict | None:
    return context.user_data.get('un')


# ---------- клавиатуры ----------

def build_menu_keyboard(counts: dict[str, int] | None = None) -> InlineKeyboardMarkup:
    counts = counts or {}

    def label(title: str, flow: str) -> str:
        return f'{title} ({counts[flow]})' if flow in counts else title

    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label('📅 Без даты', moderation.FLOW_NO_DATE), callback_data='un:q:nd')],
        [InlineKeyboardButton(label('🗂 Без категории', moderation.FLOW_NO_CATEGORY), callback_data='un:q:nc')],
        [InlineKeyboardButton('🏠 В меню', callback_data='nav:menu')],
    ])


def build_item_keyboard(flow: str, ref, left: int) -> InlineKeyboardMarkup:
    if flow == moderation.FLOW_NO_DATE:
        main = [InlineKeyboardButton('📅 Установить дату', callback_data=f'un:date:{ref}'),
                InlineKeyboardButton('🚫 Отклонить', callback_data=f'un:rej:{ref}')]
    else:
        main = [InlineKeyboardButton('🗂 Установить категорию', callback_data=f'un:cat:{ref}'),
                InlineKeyboardButton('⏭ Пропустить', callback_data=f'un:skip:{ref}')]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f'{QUEUE_TITLES[flow]} · осталось {left}', callback_data='un:noop')],
        main,
        [InlineKeyboardButton('🛑 Скрыть', callback_data=f'un:hide:{ref}')],
        [InlineKeyboardButton('⬅️ Назад', callback_data='un:menu')],
    ])


def build_category_keyboard(ref) -> InlineKeyboardMarkup:
    rows, pair = [], []
    for c in CATEGORIES:
        pair.append(InlineKeyboardButton(f'{c.emoji} {c.label}', callback_data=f'un:set:{ref}:{c.code}'))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([InlineKeyboardButton('⬅️ Назад', callback_data='un:show')])
    return InlineKeyboardMarkup(rows)


def build_date_input_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton('⬅️ Назад', callback_data='un:show')]])


# ---------- показ ----------

async def _counts(pool) -> dict[str, int]:
    out = {}
    for flow in (moderation.FLOW_NO_DATE, moderation.FLOW_NO_CATEGORY):
        try:
            out[flow] = len(await moderation.load_queue(pool, flow, EVENTS_CITY))
        except Exception as e:
            logger.warning(f"UNNAMED_COUNT_FAILED | flow={flow} | {type(e).__name__}: {e}")
    return out


async def _show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop('un_input', None)
    counts = await _counts(context.bot_data.get('pool'))
    await edit_or_send(
        context=context, chat_id=update.effective_chat.id,
        text='🧩 Неразобранные: записи, которые бот не смог разобрать.',
        keyboard=build_menu_keyboard(counts), parse_mode=None,
    )


async def _show_item(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Текущая запись очереди; записи, которых уже нет, пропускаются. Пустая очередь — сообщение."""
    pool = context.bot_data.get('pool')
    st = _state(context)
    context.user_data.pop('un_input', None)
    while st and st['ids']:
        st['i'] = min(st['i'], len(st['ids']) - 1)
        ref = st['ids'][st['i']]
        event = await fetch_event_by_id(pool, ref, update.effective_user.id)
        if not event:
            st['ids'].pop(st['i'])
            continue
        text, entities = render_full_view(event)
        await edit_or_send(
            context=context, chat_id=update.effective_chat.id, text=text,
            keyboard=build_item_keyboard(st['flow'], ref, len(st['ids'])),
            entities=entities, disable_link_preview=True,
        )
        return
    await edit_or_send(
        context=context, chat_id=update.effective_chat.id, text='✅ Очередь пуста.',
        keyboard=InlineKeyboardMarkup([[InlineKeyboardButton('⬅️ Назад', callback_data='un:menu')]]),
        parse_mode=None,
    )


async def _open_queue(update: Update, context: ContextTypes.DEFAULT_TYPE, flow: str) -> None:
    ids = await moderation.load_queue(context.bot_data.get('pool'), flow, EVENTS_CITY)
    context.user_data['un'] = {'flow': flow, 'ids': ids, 'i': 0}
    await _show_item(update, context)


# ---------- решения ----------

def _drop(context, ref) -> None:
    st = _state(context)
    if st and ref in st['ids']:
        st['ids'].remove(ref)


async def _decide(update: Update, context: ContextTypes.DEFAULT_TYPE, ref, reason: str, **fields) -> None:
    query = update.callback_query
    st = _state(context)
    if not st or ref not in st['ids']:
        await query.answer(STALE, show_alert=True)
        await _show_item(update, context) if st else await _show_menu(update, context)
        return
    pool = context.bot_data.get('pool')
    try:
        result = await moderation.decide(pool, ref, flow=st['flow'], reason_code=reason, **fields)
    except Exception as e:
        logger.error(f"UNNAMED_DECIDE_FAILED | ref={ref} | reason={reason} | {type(e).__name__}: {e}")
        await query.answer('Не удалось сохранить решение', show_alert=True)
        return
    if result == 'changed':
        await query.answer('Дата уже задана другим путём', show_alert=True)
    else:
        await query.answer()
    _drop(context, ref)
    await _show_item(update, context)


async def handle_unnamed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Все кнопки un:*."""
    query = update.callback_query
    if not is_admin_user(update.effective_user.id if update.effective_user else None):
        await query.answer('Недоступно', show_alert=True)
        return

    parts = query.data.split(':')
    action = parts[1] if len(parts) > 1 else ''

    if action == 'noop':
        await query.answer()
        return
    if action == 'menu':
        await query.answer()
        context.user_data.pop('un', None)
        await _show_menu(update, context)
        return
    if action == 'q':
        flow = QUEUE_CODES.get(parts[2]) if len(parts) > 2 else None
        if flow is None:
            await query.answer('Неверный запрос', show_alert=True)
            return
        await query.answer()
        await _open_queue(update, context, flow)
        return
    if action == 'show':
        await query.answer()
        if _state(context):
            await _show_item(update, context)
        else:
            await _show_menu(update, context)
        return

    ref = parse_ref(parts[2]) if len(parts) > 2 else None
    if ref is None:
        await query.answer('Неверный запрос', show_alert=True)
        return
    st = _state(context)
    if not st or ref not in st['ids']:
        await query.answer(STALE, show_alert=True)
        await _show_item(update, context) if st else await _show_menu(update, context)
        return

    if action == 'date':
        await query.answer()
        context.user_data['un_input'] = {'ref': ref}
        await edit_or_send(
            context=context, chat_id=update.effective_chat.id,
            text='📅 Введите дату сообщением в формате ДД.ММ.ГГГГ (например, 25.12.2026).',
            keyboard=build_date_input_keyboard(), parse_mode=None,
        )
        return
    if action == 'cat':
        await query.answer()
        await edit_or_send(
            context=context, chat_id=update.effective_chat.id, text='🗂 Выберите категорию:',
            keyboard=build_category_keyboard(ref), parse_mode=None,
        )
        return
    if action == 'set':
        code = parts[3] if len(parts) > 3 else ''
        label = next((c.label for c in CATEGORIES if c.code == code), None)
        if label is None:
            await query.answer('Неизвестная категория', show_alert=True)
            return
        await _decide(update, context, ref, 'set_category', correct_category=label)
        return
    if action == 'rej':
        await _decide(update, context, ref, 'rejected')
        return
    if action == 'skip':
        await _decide(update, context, ref, 'skipped')
        return
    if action == 'hide':
        await _decide(update, context, ref, 'hidden')
        return

    await query.answer('Неверный запрос', show_alert=True)


async def handle_unnamed_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Текст админа, пока ожидается дата. True — сообщение обработано."""
    state = context.user_data.get('un_input')
    if not state or not is_admin_user(update.effective_user.id if update.effective_user else None):
        return False

    day = parse_review_date((update.message.text or '').strip())
    if day is None:
        await update.message.reply_text('❌ Неверная дата. Введите в формате ДД.ММ.ГГГГ или нажмите «Назад».')
        return True

    ref = state['ref']
    st = _state(context)
    try:
        await update.message.delete()
    except Exception:
        pass
    if not st or ref not in st['ids']:
        context.user_data.pop('un_input', None)
        await update.message.reply_text(f'❌ {STALE}')
        return True
    try:
        result = await moderation.decide(
            context.bot_data.get('pool'), ref, flow=st['flow'], reason_code='set_date', correct_date=day)
    except Exception as e:
        logger.error(f"UNNAMED_DATE_FAILED | ref={ref} | {type(e).__name__}: {e}")
        await update.message.reply_text('❌ Не удалось сохранить дату. Попробуйте ещё раз.')
        return True
    _drop(context, ref)
    if result == 'changed':
        await update.message.reply_text('ℹ️ Дата у записи уже задана, ничего не изменено.')
    await _show_item(update, context)
    return True
