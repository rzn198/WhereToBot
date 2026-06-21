from telegram import InlineKeyboardMarkup, InlineKeyboardButton


def get_event_nav_keyboard(
    current_index: int,
    total: int,
    surrogate_id: int,
    is_favorite: bool,
    is_preview: bool = False,
) -> InlineKeyboardMarkup:

    counter_or_expand = (
        InlineKeyboardButton('📋 Полностью', callback_data='nav:expand')
        if is_preview else
        InlineKeyboardButton(f'{current_index + 1} / {total}', callback_data='nav:counter')
    )

    fav_btn = InlineKeyboardButton(
        '❤️ В избранное' if not is_favorite else '❤️ Убрать',
        callback_data=f'fav:{"add" if not is_favorite else "remove"}:{surrogate_id}'
    )

    hide_btn = InlineKeyboardButton(
        '🚫 Скрыть',
        callback_data=f'hide:{surrogate_id}'
    )

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton('🔙', callback_data='nav:prev'),
            counter_or_expand,
            InlineKeyboardButton('🔜', callback_data='nav:next'),
        ],
        [fav_btn, hide_btn],
        [
            InlineKeyboardButton('🏠 В меню', callback_data='nav:menu'),
        ],
    ])