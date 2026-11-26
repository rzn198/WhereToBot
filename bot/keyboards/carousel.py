from telegram import InlineKeyboardButton


def carousel_row(index: int, total: int, prev_data: str, next_data: str, next_prefix: str = '') -> list[InlineKeyboardButton]:
    """Стрелки карусели: две кнопки, на каждой номер карточки, куда она ведёт («◀️ 8/22» · «10/22 ▶️»).
    Листание по кругу: с первой карточки «назад» ведёт на последнюю, с последней «вперёд» — на первую.
    next_prefix — слово перед номером на правой кнопке («Пропустить»). Одна карточка — пустой список (стрелки не нужны)."""
    if total <= 1:
        return []
    prev_no = (index - 1) % total + 1
    next_no = (index + 1) % total + 1
    return [
        InlineKeyboardButton(f'◀️ {prev_no}/{total}', callback_data=prev_data),
        InlineKeyboardButton(f'{next_prefix}{next_no}/{total} ▶️', callback_data=next_data),
    ]
