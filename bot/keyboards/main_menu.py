from telegram import InlineKeyboardMarkup, InlineKeyboardButton
from shared.config import config

def get_main_menu_inline(is_admin: bool = False) -> InlineKeyboardMarkup:
    flags = config.get('feature_flags', {})

    buttons = [
        [
            InlineKeyboardButton('📅 Сегодня',           callback_data='filter:today'),
            InlineKeyboardButton('📅 Завтра',             callback_data='filter:tomorrow'),
        ],
        [
            InlineKeyboardButton('📅 На этой неделе',    callback_data='filter:week'),
            InlineKeyboardButton('📅 На дату', callback_data='filter:calendar'),
        ],
        [
            InlineKeyboardButton('⭐ Избранное',          callback_data='filter:favorites'),
        ],
        [
            InlineKeyboardButton('🎬 Киноафиша', callback_data='cinema:menu'),
        ],
    ]

    if flags.get('show_contact_button'):
        buttons.append([
            InlineKeyboardButton('✉️ Связь с автором',   callback_data='contact:start'),
        ])

    if is_admin:
        buttons.append([
            InlineKeyboardButton('📬 Диалоги',            callback_data='admin:dialogs'),
        ])
        buttons.append([
            InlineKeyboardButton('🔍 Дубли',              callback_data='admin:duplicates'),
        ])
        buttons.append([
            InlineKeyboardButton('⚙️ Админка',            callback_data='admin_panel:main'),
        ])

    donate_cfg = config.get('donate', {})
    if donate_cfg.get('enabled'):
        buttons.append([
            InlineKeyboardButton(
                donate_cfg.get('button_text', '💰 Поддержать'),
                callback_data='donate'
            ),
        ])

    return InlineKeyboardMarkup(buttons)


MENU_TEXT = "🗓 *Афиша Белграда*\n\n-Выбирай развлечение по душе, от концерта до сеанса психологической помощи:\n\n-Заноси в избранное:\n\n-Каждое утро получай напоминание о событиях, которые у тебя на сегодня в избранном\n\n-Киноафиша - подробное распиание всех сеансов в Белграде"