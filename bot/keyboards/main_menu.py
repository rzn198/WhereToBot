from telegram import InlineKeyboardMarkup, InlineKeyboardButton
from bot.utils.categories import MAIN_MENU_CODES, get_category
from shared.config import config

def _n(counts: dict | None, key: str) -> str:
    return f' ({counts[key]})' if counts and key in counts else ''


def get_main_menu_inline(is_admin: bool = False, counts: dict | None = None) -> InlineKeyboardMarkup:
    """counts — {код категории: число, 'rest': число}; показывается только админу (обычным пользователям счётчиков нет)."""
    counts = counts if is_admin else None
    flags = config.get('feature_flags', {})

    # категории из MAIN_MENU_CODES — отдельными кнопками (по одной в ряд), остальные за «Остальные события»
    buttons = [
        [InlineKeyboardButton(f'{c.emoji} {c.label}{_n(counts, c.code)}', callback_data=f'cat:{c.code}')]
        for c in (get_category(code) for code in MAIN_MENU_CODES) if c is not None
    ]
    buttons += [
        [
            InlineKeyboardButton(f"🗂 Остальные события{_n(counts, 'rest')}", callback_data='cats:menu')
        ],
        [
            InlineKeyboardButton('⭐ Избранное', callback_data='filter:favorites')
        ],
        [
            InlineKeyboardButton('🎬 Афиша кинотеатров', callback_data='cinema:menu'),
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


MENU_TEXT = "🗓 *Афиша Белграда*\n\n-Выбирай развлечение по душе, от концерта до сеанса психологической помощи:\n\n-Заноси в избранное:\n\n-Каждое утро получай напоминание о событиях, которые у тебя на сегодня в избранном\n\n-Афиша кинотеатров - подробное расписание всех сеансов в Белграде"