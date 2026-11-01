"""Ссылки на элементы единой ленты.

Лента показывает и посты каналов (таблица messages), и события сайтов
(таблица events). Элемент ленты — «ссылка»:
    пост    -> int         (messages.surrogate_id), как и раньше
    событие -> 'e<id>'     (events.id)

Формат callback_data: fav:add:123 / fav:add:e45 / hide:123 / hide:e45.
Старые кнопки в чатах пользователей (только числа) продолжают работать.
"""

from __future__ import annotations

Ref = int | str

EVENT_PREFIX = 'e'


def event_ref(event_id: int) -> str:
    return f'{EVENT_PREFIX}{int(event_id)}'


def is_event_ref(ref: Ref) -> bool:
    return isinstance(ref, str) and ref.startswith(EVENT_PREFIX)


def parse_ref(raw: str | int | None) -> Ref | None:
    """'123' -> 123, 'e45' -> 'e45', мусор -> None."""
    if raw is None:
        return None
    if isinstance(raw, int):
        return raw
    raw = str(raw).strip()
    if raw.isdigit():
        return int(raw)
    if raw.startswith(EVENT_PREFIX) and raw[1:].isdigit():
        return event_ref(int(raw[1:]))
    return None


def ref_id(ref: Ref) -> int:
    """Числовой id внутри своей таблицы."""
    return int(ref[1:]) if is_event_ref(ref) else int(ref)
