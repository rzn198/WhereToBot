"""Экран «Дубли» для методов deduplication.method = ai | shadow: решения AI по одному.

  ai     — действующие решения (active), кнопки «Вернуть», «Поменять местами», «Пропустить»;
  shadow — предложенные решения (proposed), только просмотр: ничего не скрыто, действовать не с чем.
Для legacy остаётся прежний экран (admin_duplicates.py). Выбор — в handle_admin_duplicates.
Сначала свежие решения. Все действия — через db/dedup_admin.py (флаги видимости и решения меняются вместе).
"""
from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from bot.handlers.contact import _edit_contact_window, _require_admin, escape_md
from db import dedup_admin as da

PREVIEW_CHARS = 600


def _src(rec: dict | None) -> str:
    return escape_md(rec['source']) if rec else 'запись удалена'


def _txt(rec: dict | None) -> str:
    return escape_md(rec['text'][:PREVIEW_CHARS]) if rec else ''


async def start_decisions(update: Update, context: ContextTypes.DEFAULT_TYPE, method: str) -> None:
    """Точка входа из handle_admin_duplicates (админ уже проверен)."""
    pool = context.bot_data.get('pool')
    status = 'active' if method == 'ai' else 'proposed'
    ids = await da.list_decision_ids(pool, status)
    context.user_data['dd_ids'] = ids
    context.user_data['dd_idx'] = 0
    context.user_data['dd_status'] = status
    await _show(update, context)


async def _show(update: Update, context: ContextTypes.DEFAULT_TYPE, notice: str | None = None) -> None:
    pool = context.bot_data.get('pool')
    chat_id = update.effective_chat.id
    menu_message_id = context.user_data.get('menu_message_id')
    ids: list[int] = context.user_data.get('dd_ids', [])
    status = context.user_data.get('dd_status', 'active')
    idx = context.user_data.get('dd_idx', 0)

    data = None
    while idx < len(ids):                      # решения, которые успели измениться или исчезнуть, пропускаем
        data = await da.get_decision(pool, ids[idx])
        if data and data['decision']['status'] == status:
            break
        ids.pop(idx)
        data = None
    context.user_data['dd_idx'] = idx

    back = [InlineKeyboardButton('🏠 В меню', callback_data='nav:menu')]
    if data is None:
        empty = '✅ Решений нет.' if not ids else '✅ Все решения разобраны.'
        await _edit_contact_window(context=context, chat_id=chat_id, menu_message_id=menu_message_id,
                                   text=(f"{escape_md(notice)}\n\n" if notice else '') + empty,
                                   keyboard=InlineKeyboardMarkup([back]))
        return

    d, w, l = data['decision'], data['winner'], data['loser']
    head = f"🧩 *Дубль {idx + 1} из {len(ids)}* · {d['day']}"
    if status == 'proposed':
        head += "\n👀 _shadow: это предложения, ничего не скрыто_"
    lines = [head, '']
    lines.append(f"*Победитель* (виден) — {_src(w)}\n{_txt(w)}")
    lines.append('')
    lines.append(f"*{'Предложено скрыть' if status == 'proposed' else 'Скрыт'}* — {_src(l)}\n{_txt(l)}")
    if d['reason']:
        lines.append(f"\nПричина: {escape_md(d['reason'])}")
    meta = f"Модель: {escape_md(d['model'] or '—')} · {d['created_at']:%d.%m %H:%M}"
    if d['decided_by'] == 'admin':
        meta += ' · ✋ решение админа'
    lines.append(meta)
    text = ((f"✅ {escape_md(notice)}\n\n") if notice else '') + '\n'.join(lines)

    rows = []
    if status == 'active':
        row = [InlineKeyboardButton('↩️ Вернуть', callback_data=f"dd:rv:{d['id']}")]
        if await da.can_swap(pool, d, w, l):
            row.append(InlineKeyboardButton('🔁 Поменять местами', callback_data=f"dd:sw:{d['id']}"))
        rows.append(row)
    rows.append([
        InlineKeyboardButton('◀️', callback_data='dd:nav:prev'),
        InlineKeyboardButton(f'{idx + 1}/{len(ids)}', callback_data='dd:nav:counter'),
        InlineKeyboardButton('Пропустить ▶️' if status == 'active' else '▶️', callback_data='dd:nav:next'),
    ])
    rows.append(back)
    await _edit_contact_window(context=context, chat_id=chat_id, menu_message_id=menu_message_id,
                               text=text, keyboard=InlineKeyboardMarkup(rows))


async def handle_dd_nav(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not await _require_admin(update):
        return
    direction = query.data.split(':')[2]
    ids = context.user_data.get('dd_ids', [])
    idx = context.user_data.get('dd_idx', 0)
    if direction == 'prev' and idx > 0:
        context.user_data['dd_idx'] = idx - 1
    elif direction == 'next' and idx < len(ids) - 1:
        context.user_data['dd_idx'] = idx + 1
    await _show(update, context)


async def handle_dd_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """dd:rv:<id решения> — вернуть, dd:sw:<id> — поменять местами."""
    query = update.callback_query
    await query.answer()
    if not await _require_admin(update):
        return
    parts = query.data.split(':')
    if len(parts) != 3 or not parts[2].isdigit() or parts[1] not in ('rv', 'sw'):
        await query.answer('Неверный запрос', show_alert=True)
        return
    pool = context.bot_data.get('pool')
    decision_id = int(parts[2])
    admin_id = update.effective_user.id
    if parts[1] == 'rv':
        outcome = await da.restore_loser(pool, decision_id, admin_id)
    else:
        outcome = await da.swap_decision(pool, decision_id, admin_id)

    # решение обработано (или устарело): убираем из списка, следующее встаёт на то же место.
    # Смена местами создаёт новое решение админа — оно в этот список не попадает, найдём его при повторном открытии.
    ids = context.user_data.get('dd_ids', [])
    if decision_id in ids:
        ids.remove(decision_id)
    await _show(update, context, notice=outcome.text if outcome.ok else f"⚠️ {outcome.text}")
