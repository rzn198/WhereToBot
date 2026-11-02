"""Карточка события из таблицы events для ленты бота.

У событий сайтов нет Telegram-форматирования (entities), поэтому карточка
собирается из полей: название жирным, дата, место, описание и ссылка на
источник. Смещения entities считаются в UTF-16, как требует Bot API.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import urlparse

from telegram import MessageEntity

from collector.entities.utf16 import utf16_len

MONTHS_GEN = [
    '', 'января', 'февраля', 'марта', 'апреля', 'мая', 'июня',
    'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря',
]
WEEKDAYS = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс']

PREVIEW_SUFFIX = 'Полное описание события⬇️⬇️⬇️📋'
FULL_DESCRIPTION_LIMIT = 3000      # запас до 4096 символов сообщения
CAPTION_LIMIT = 1024               # лимит подписи к фото

# перевод description_ru хранится как Markdown: экранированный текст + ссылка
_RU_LINK_TAIL = re.compile(r'\s*\[[^\]]*\]\([^)]*\)\s*$')
_MD_ESCAPED = re.compile(r'\\([_*`\[\]])')
_MANY_NEWLINES = re.compile(r'\n{3,}')


@dataclass
class EventCard:
    text: str
    entities: list[MessageEntity] = field(default_factory=list)
    photo: str | None = None
    is_preview: bool = False


def format_date_ru(d: date, today: date | None = None) -> str:
    """'Пт, 13 ноября' (с годом, если он не текущий)."""
    today = today or date.today()
    s = f'{WEEKDAYS[d.weekday()]}, {d.day} {MONTHS_GEN[d.month]}'
    return s if d.year == today.year else f'{s} {d.year}'


def plain_description(row: dict) -> str:
    """Описание для показа: русский перевод, если есть, иначе оригинал."""
    ru = (row.get('description_ru') or '').strip()
    if ru:
        text = _MD_ESCAPED.sub(r'\1', _RU_LINK_TAIL.sub('', ru))
    else:
        text = (row.get('description') or '')
    text = text.replace('\r\n', '\n').strip()
    return _MANY_NEWLINES.sub('\n\n', text)


def _cut(text: str, limit: int) -> str:
    """Обрезка по границе слова + многоточие."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    sp = cut.rfind(' ')
    if sp > limit * 0.6:
        cut = cut[:sp]
    return cut.rstrip(' ,.;:-—\n') + '…'


def source_label(url: str | None) -> str:
    host = urlparse(url or '').netloc.lower()
    return host[4:] if host.startswith('www.') else host


class _Builder:
    """Склеивает текст и считает UTF-16-смещения entities."""

    def __init__(self):
        self.parts: list[str] = []
        self.entities: list[MessageEntity] = []
        self._len16 = 0

    def add(self, text: str, kind: str | None = None, url: str | None = None):
        if not text:
            return
        if kind:
            kw = {'type': kind, 'offset': self._len16, 'length': utf16_len(text)}
            if url:
                kw['url'] = url
            self.entities.append(MessageEntity(**kw))
        self.parts.append(text)
        self._len16 += utf16_len(text)

    @property
    def text(self) -> str:
        return ''.join(self.parts)


def build_event_card(
    row: dict,
    expanded: bool = False,
    preview_chars: int = 400,
    today: date | None = None,
) -> EventCard:
    description = plain_description(row)
    photo = row.get('image_file_id') or row.get('images') or None

    too_long = len(description) > preview_chars
    is_preview = (too_long or bool(photo)) and not expanded

    if is_preview:
        shown = _cut(description, preview_chars)
    else:
        shown = _cut(description, FULL_DESCRIPTION_LIMIT)

    b = _Builder()
    b.add((row.get('title') or '').strip(), MessageEntity.BOLD)
    b.add('\n')
    if row.get('starts_at'):
        b.add(f"📅 {format_date_ru(row['starts_at'], today)}")
    venue = (row.get('venue') or '').strip()
    if venue:
        b.add(f"\n📍 {venue}")
    if shown:
        b.add('\n\n' + shown)

    url = row.get('source_url')
    if url:
        label = source_label(url)
        b.add('\n\n')
        b.add(f"🎟 Билеты и подробности · {label}" if label else '🎟 Билеты и подробности',
              MessageEntity.TEXT_LINK, url)

    if is_preview:
        b.add('\n\n')
        b.add(PREVIEW_SUFFIX, MessageEntity.ITALIC)

    text = b.text
    # фото показываем только в полном виде и если подпись помещается
    show_photo = (photo if (not is_preview and len(text) <= CAPTION_LIMIT) else None)
    return EventCard(text=text, entities=b.entities, photo=show_photo, is_preview=is_preview)
