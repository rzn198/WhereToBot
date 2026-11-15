from loguru import logger
from telegram import Update
from telegram.ext import ContextTypes

from db import fav_transfer
from db.tables import Tables
from bot.handlers.events import show_current_event
from bot.utils.refs import event_ref, is_event_ref, parse_ref, ref_id


async def _target(conn, ref):
    """Куда писать избранное. Берём строку записи под FOR SHARE: пока мы её держим, дедупликатор не может скрыть
    запись (его UPDATE ждёт конца нашей транзакции), а если запись уже скрыта как дубль — избранное
    пишется победителю. Возвращает ссылку победителя либо исходную."""
    kind = 'e' if is_event_ref(ref) else 'm'
    table, col = (Tables.EVENTS, 'id') if kind == 'e' else (Tables.MESSAGES, 'surrogate_id')
    row = await conn.fetchrow(f"SELECT hidden_by_admin FROM {table} WHERE {col} = $1 FOR SHARE", ref_id(ref))
    if row is None or not row['hidden_by_admin']:
        return ref
    wk, wi = await fav_transfer.resolve_winner(conn, kind, ref_id(ref))
    return event_ref(wi) if wk == 'e' else wi


async def handle_favorites(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    await query.answer()

    pool = context.bot_data.get('pool')
    user_id = update.effective_user.id
    data = query.data

    parts = data.split(':')
    ref = parse_ref(parts[2]) if len(parts) == 3 else None
    if ref is None:
        await query.answer('Неверный запрос', show_alert=True)
        return

    action = parts[1]
    if action == 'add':
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    target = await _target(conn, ref)
                    t_table, t_column = (
                        (Tables.EVENT_FAVORITES, 'event_id') if is_event_ref(target)
                        else (Tables.FAVORITES, 'msg_id')
                    )
                    await conn.execute(f"""
                        INSERT INTO {t_table} (user_id, {t_column})
                        VALUES ($1, $2)
                        ON CONFLICT DO NOTHING
                    """, user_id, ref_id(target))
            if target != ref:                         # открыли карточку проигравшего дубля: избранное ушло победителю
                _replace_current(context, ref, target)
            logger.info(
                f"FAV_ADD | user_id={user_id} | ref={ref}" + (f" | winner={target}" if target != ref else "")
            )
            await query.answer('⭐ Добавлено в избранное', show_alert=False)
        except Exception as e:
            logger.error(f"FAV_ADD error: {e}")
            await query.answer('Ошибка. Попробуй ещё раз.', show_alert=True)

    elif action == 'remove':
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    target = await _target(conn, ref)
                    t_table, t_column = (
                        (Tables.EVENT_FAVORITES, 'event_id') if is_event_ref(target)
                        else (Tables.FAVORITES, 'msg_id')
                    )
                    await conn.execute(f"""
                        DELETE FROM {t_table}
                        WHERE user_id = $1 AND {t_column} = $2
                    """, user_id, ref_id(target))
            if target != ref:
                _replace_current(context, ref, target)
            logger.info(
                f"FAV_REMOVE | user_id={user_id} | ref={ref}" + (f" | winner={target}" if target != ref else "")
            )
            await query.answer('Убрано из избранного', show_alert=False)
        except Exception as e:
            logger.error(f"FAV_REMOVE error: {e}")
            await query.answer('Ошибка. Попробуй ещё раз.', show_alert=True)

    await show_current_event(update, context, pool)


def _replace_current(context: ContextTypes.DEFAULT_TYPE, old, new) -> None:
    """В открытом списке заменить проигравший дубль победителем, чтобы карточка и кнопки относились к нему."""
    ids = context.user_data.get('event_ids') or []
    for i, r in enumerate(ids):
        if r == old:
            ids[i] = new
