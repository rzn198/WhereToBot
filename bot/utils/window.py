"""Окно категории: строки списка, страницы и раскладка дня и периода (чистая логика, без БД и Telegram).

Строка режима «code»: `N. Название · 21:00 · Место`, вся строка после номера ссылка на карточку (ссылка собирается снаружи).
Название события берётся из events.title, у поста названия нет: берётся первая осмысленная строка текста без ссылок,
@упоминаний и #тегов. Время есть пока у ticketsrs, место только у событий сайтов (показывается, если ui.window.show_venue: true).

Раскладка:
  день и небольшой период — список по страницам (по умолчанию 15 событий на страницу), листание по кругу;
  большой период          — сводка по дням (число событий) и кнопка на каждый день.
Значения настроек ограничены так, что страница всегда помещается в сообщение Telegram (4096 символов).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from html import escape
from typing import Callable

from card_lines.settings import CardLineSettings, display_settings
from shared.config import config

SEP = ' · '
BULLET = '•'                       # маркер строки списка (номера нет: ссылка ведёт на карточку, а не на номер)
ELLIPSIS = '…'
DAY_BUTTONS_PER_ROW = 3             # кнопки дней в сводке периода

WEEKDAYS = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс']
MONTHS_GEN = ['', 'января', 'февраля', 'марта', 'апреля', 'мая', 'июня',
              'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря']

_URL = re.compile(r'https?://\S+|t\.me/\S+|www\.\S+', re.I)
_TAGS = re.compile(r'[#@]\w+', re.U)
_LEADING_JUNK = re.compile(r'^[^\w«"“(\[]+', re.U)
_MARKUP = re.compile(r'[*_`~|]+')
_SPACES = re.compile(r'\s+')


@dataclass(frozen=True)
class WindowSettings:
    page_size: int = 15          # событий на странице; больше — листание
    title_max: int = 40
    venue_max: int = 22
    show_venue: bool = False     # место в строке (после времени); по умолчанию не показываем
    period_list_max: int = 45    # период с большим числом событий показывается сводкой по дням, а не списком


def _int(value, default: int, low: int, high: int) -> int:
    try:
        return min(max(int(value), low), high)
    except (TypeError, ValueError):
        return default


def _bool(value, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return {'true': True, 'yes': True, 'on': True, '1': True, 'false': False, 'no': False, 'off': False, '0': False}.get(
            value.strip().lower(), default)
    return default if value is None else bool(value)


def window_settings() -> WindowSettings:
    """Лимиты из config.yaml → ui.window (необязательно); мусор заменяется значениями по умолчанию.
    Верхние границы подобраны так, что страница (page_size строк по title_max + venue_max) всегда в пределах 4096 символов."""
    raw = (config.get('ui') or {}).get('window') or {}
    d = WindowSettings()
    return WindowSettings(
        page_size=_int(raw.get('page_size'), d.page_size, 5, 20),
        title_max=_int(raw.get('title_max'), d.title_max, 10, 80),
        venue_max=_int(raw.get('venue_max'), d.venue_max, 5, 40),
        show_venue=_bool(raw.get('show_venue'), d.show_venue),
        period_list_max=_int(raw.get('period_list_max'), d.period_list_max, 10, 200),
    )


def truncate(text: str, limit: int) -> str:
    """Обрезка по границе слова (если оно не слишком далеко) с многоточием."""
    if len(text) <= limit:
        return text
    cut = text[:limit - 1]
    space = cut.rfind(' ')
    if space >= limit * 0.6:
        cut = cut[:space]
    return cut.rstrip(' ,.;:-–—') + ELLIPSIS


def event_title(title: str | None) -> str:
    return _SPACES.sub(' ', title or '').strip()


_WEEKDAY = r'(?:понедельник|вторник|среда|четверг|пятница|суббота|воскресенье|пон|вто|сре|чет|пят|суб|вос|пн|вт|ср|чт|пт|сб|вс)\.?'
_MONTH = (r'(?:январ[яь]|феврал[яь]|март[а]?|апрел[яь]|ма[яй]|июн[яь]|июл[яь]|август[а]?|сентябр[яь]|октябр[яь]|'
          r'ноябр[яь]|декабр[яь])')
_DATE = rf'(?:\d{{1,2}}[./-]\d{{1,2}}(?:[./-]\d{{2,4}})?\.?|\d{{1,2}}\s+{_MONTH}(?:\s+\d{{4}})?(?:\s*г\.?)?)'
_SEPARATOR = r'[\s,;:–—-]+'
# Строка-заголовок дня: «Среда», «Среда, 18 ноября», «18 ноября», «Ср 18.11», «Сегодня, 18 ноября» (и ничего больше)
_DATE_HEADER = re.compile(
    rf'^(?:(?:{_WEEKDAY}|сегодня|завтра)(?:{_SEPARATOR}{_DATE})?|{_DATE}(?:{_SEPARATOR}{_WEEKDAY})?)[^\w]*$',
    re.I | re.U,
)


def _clean_line(line: str) -> str:
    s = _MARKUP.sub('', _TAGS.sub('', _URL.sub('', line)))
    return _SPACES.sub(' ', _LEADING_JUNK.sub('', s.strip())).strip()


def post_title(text: str | None) -> str:
    """Название поста: первая осмысленная строка (не короче 3 символов), заголовки дня («Среда, 18 ноября») пропускаются:
    событие у таких постов под заголовком. Если кроме заголовков ничего нет, берётся первый заголовок."""
    header = ''
    for line in (text or '').splitlines():
        s = _clean_line(line)
        if len(s) < 3:
            continue
        if _DATE_HEADER.match(s):
            header = header or s
            continue
        return s
    return header


def short_venue(venue: str | None, limit: int) -> str:
    """Место до первой запятой (адрес не нужен), не длиннее limit."""
    first = (venue or '').split(',')[0]
    return truncate(_SPACES.sub(' ', first).strip(), limit)


@dataclass(frozen=True)
class Item:
    kind: str               # 'm' пост | 'e' событие сайта
    id: int
    day: date
    title: str              # события: events.title, посты: начало текста
    venue: str | None = None
    time: object = None     # datetime.time | None
    ai_title: str | None = None     # ИИ-строка (card_lines): короткое название
    ai_attr: str | None = None      # значение атрибута (жанр, формат...)
    ai_profile: str | None = None   # профиль, по которому строка сгенерирована (нужен для эмодзи)
    ai_hook: str | None = None      # хук (фраза после названия)
    ai_time: object = None          # время старта из текста записи (если в БД starts_time пусто)

    @property
    def ref(self) -> int | str:
        return f'e{self.id}' if self.kind == 'e' else self.id


def item_from_row(row) -> Item:
    keys = row.keys()
    return Item(kind=row['kind'], id=row['id'], day=row['d'], title=row['title'] or '', venue=row['venue'], time=row['tm'],
                ai_title=row['ai_title'] if 'ai_title' in keys else None,
                ai_attr=row['ai_attr'] if 'ai_attr' in keys else None,
                ai_profile=row['ai_profile'] if 'ai_profile' in keys else None,
                ai_hook=row['ai_hook'] if 'ai_hook' in keys else None,
                ai_time=row['ai_time'] if 'ai_time' in keys else None)


LINE_CAP = 190      # видимых символов в строке с хуком: страница из 20 строк остаётся в пределах 4096


def line_text(item: Item, st: WindowSettings, opts: CardLineSettings | None = None) -> str:
    """Текст строки без маркера.
    ИИ-строка и card_lines.method on: `🎸 Название — хук · 21:00`; если хука нет (или card_lines.hooks выключены) —
    `🎸 Название · жанр · 21:00` (эмодзи и жанр только если известны); иначе строка из данных БД: `Название · 21:00 · Место`.
    Время: из БД, а если там пусто, то явное время из текста записи."""
    use_ai = bool(opts is not None and opts.use_in_bot and item.ai_title)
    when = item.time if item.time is not None else (item.ai_time if use_ai else None)
    if use_ai:
        title = truncate(event_title(item.ai_title), st.title_max)
        prof = opts.profiles.get(item.ai_profile or '')
        emoji = prof.emoji_for(item.ai_attr) if (opts.emoji and prof is not None) else ''
        head = f'{emoji} {title}' if emoji and title else title or 'Без названия'
        hook = (item.ai_hook or '').strip() if opts.use_hooks else ''
        tail = f' · {when.strftime("%H:%M")}' if when is not None else ''
        if hook:
            room = LINE_CAP - len(head) - len(' — ') - len(tail)
            if room >= 12:
                return f'{head} — {truncate(hook, min(room, opts.hooks.hook_max))}{tail}'
        parts = [head]
        if item.ai_attr and opts.show_value:
            parts.append(item.ai_attr.replace(',', ', '))
    else:
        title = event_title(item.title) if item.kind == 'e' else post_title(item.title)
        parts = [truncate(title, st.title_max) or 'Без названия']
    if when is not None:
        parts.append(when.strftime('%H:%M'))
    venue = short_venue(item.venue, st.venue_max) if item.kind == 'e' and st.show_venue else ''
    if venue:
        parts.append(venue)
    return SEP.join(parts)


def build_line(number: int, item: Item, st: WindowSettings, opts: CardLineSettings | None = None) -> str:
    return f'{BULLET} {line_text(item, st, opts)}'


def format_day_title(day: date, today: date) -> str:
    """'Сб, 21 ноября' (с годом, если он не текущий)."""
    s = f'{WEEKDAYS[day.weekday()]}, {day.day} {MONTHS_GEN[day.month]}'
    return s if day.year == today.year else f'{s} {day.year}'


def format_day_button(day: date, count: int) -> str:
    return f'{WEEKDAYS[day.weekday()]} {day.day:02d}.{day.month:02d} · {count}'


def plural_events(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        word = 'событие'
    elif n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        word = 'события'
    else:
        word = 'событий'
    return f'{n} {word}'


@dataclass(frozen=True)
class Row:
    kind: str                       # 'day' — заголовок дня; 'item' — событие; 'text' — пояснение
    text: str
    number: int = 0
    item: Item | None = None


@dataclass
class Layout:
    """Результат раскладки. mode: empty | list | aggregate."""
    mode: str
    head: str                       # заголовок вместе с числом событий и страницей
    rows: list[Row] = field(default_factory=list)
    page: int = 1
    pages: int = 1
    day_counts: list[tuple[date, int]] = field(default_factory=list)
    total: int = 0

    @property
    def shown(self) -> list[Item]:
        return [r.item for r in self.rows if r.kind == 'item']


def group_by_day(items: list[Item]) -> list[tuple[date, list[Item]]]:
    days: list[tuple[date, list[Item]]] = []
    for it in items:
        if days and days[-1][0] == it.day:
            days[-1][1].append(it)
        else:
            days.append((it.day, [it]))
    return days


def page_count(total: int, st: WindowSettings) -> int:
    return max(1, -(-total // st.page_size))


def layout_window(items: list[Item], head: str, first: date, last: date, today: date, st: WindowSettings,
                  page: int = 1, opts: CardLineSettings | None = None) -> Layout:
    """Окно за [first, last]. head — заголовок («🎵 Концерты · Сб, 21 ноября»); page — номер страницы с 1 (вне диапазона → 1)."""
    total = len(items)
    if not total:
        return Layout('empty', head)
    if opts is None:
        opts = display_settings()
    days = group_by_day(items)
    counts = [(d, len(g)) for d, g in days]

    if first != last and len(days) > 1 and total > st.period_list_max:
        rows = [Row('text', 'Период большой для списка, выберите день:'), Row('text', '')]
        rows += [Row('day', f'{format_day_title(d, today)} · {plural_events(c)}') for d, c in counts]
        return Layout('aggregate', f'{head} · {plural_events(total)}', rows, 1, 1, counts, total)

    pages = page_count(total, st)
    if not 1 <= page <= pages:
        page = 1
    start = (page - 1) * st.page_size
    several_days = len(days) > 1
    rows: list[Row] = []
    prev_day = None
    for n, it in enumerate(items[start:start + st.page_size], start + 1):
        if several_days and it.day != prev_day:
            rows.append(Row('day', format_day_title(it.day, today)))
            prev_day = it.day
        rows.append(Row('item', line_text(it, st, opts), n, it))
    title = f'{head} · {plural_events(total)}' + (f' · стр. {page}/{pages}' if pages > 1 else '')
    return Layout('list', title, rows, page, pages, counts, total)


def render_text(layout: Layout, url_for: Callable[[Item], str | None] | None = None) -> tuple[str, str | None]:
    """(текст, parse_mode). Со ссылками (url_for вернул адрес) текст в HTML, иначе обычный текст без разметки."""
    html = url_for is not None
    esc = escape if html else (lambda s: s)
    lines = [esc(layout.head), '']
    for r in layout.rows:
        if r.kind == 'item':
            url = url_for(r.item) if html else None
            body = f'<a href="{url}">{esc(r.text)}</a>' if url else esc(r.text)
            lines.append(f'{BULLET} {body}')
        elif r.kind == 'day' and layout.mode == 'list':
            if lines[-1] != '':
                lines.append('')
            lines.append(f'<b>{esc(r.text)}</b>' if html else r.text)
        else:
            lines.append(esc(r.text))
    return '\n'.join(lines).rstrip('\n'), ('HTML' if html else None)
