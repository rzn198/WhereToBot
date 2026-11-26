"""Тексты и раскладка окна киноафиши (чистая логика, без БД и Telegram).

Название фильма: английское от AI (cinema_movies.title_en), пока AI не обработал фильм — очищенное сербское (без формата
«3D», «(sinhro.)», капслока). Вторая строка карточки — русское название, если оно есть и отличается.
Описание: перевод (description_ru), пока перевода нет — исходное. Превью режется по границе слова, полное — целиком.
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from html import escape

from bot.utils.window import format_day_title, truncate
from shared.tz import BELGRADE_TZ
from translator.text import truncate_word_boundary

PREVIEW_CHARS = 300              # превью описания на карточке
FULL_MAX_CHARS = 3500            # полное описание: всё, что влезает в сообщение Telegram вместе с заголовком
POSTER_SOURCES = ('cineplexx', 'cinestar', 'cinegrand', 'mts_dvorana', 'tuckwood', 'roda')   # чей постер берём первым

_FORMAT_PAREN = re.compile(r'\s*[\(\[]\s*(?:sinhro\.?|sinhronizovano|dubbed|dub\.?|titlovano|titl\.?)\s*[\)\]]', re.I)
_FORMAT_WORD = re.compile(r'\s*[-–—]?\s*\b(?:[234]d|imax|4dx|screenx)\b', re.I)
_SPACES = re.compile(r'\s+')
_WORD_START = re.compile(r"(?<![\w'’])(\w)(\w*)", re.U)
_BLANKS = re.compile(r'\n{3,}')
_COLON_SPACE_BEFORE = re.compile(r'\s+:')
_COLON_LETTER_AFTER = re.compile(r':(?=[^\W\d_])')


def clean_title(raw: str | None) -> str:
    """Запасное название до работы AI: без формата («3D», «(sinhro.)»), КАПС → Обычный Регистр."""
    t = _FORMAT_PAREN.sub('', raw or '')
    t = _FORMAT_WORD.sub('', t)
    t = _SPACES.sub(' ', t).strip(' -–—:·')
    t = _COLON_SPACE_BEFORE.sub(':', t)
    t = _COLON_LETTER_AFTER.sub(': ', t)
    letters = [c for c in t if c.isalpha()]
    if letters and all(c.isupper() for c in letters):
        t = _WORD_START.sub(lambda m: m.group(1).upper() + m.group(2), t.lower())
    return t


def display_title(movie) -> str:
    return (movie['title_en'] or '').strip() or clean_title(movie['title_serbian'] or movie['title_original']) or 'Без названия'


def ru_title(movie) -> str:
    """Русское название для второй строки; пусто, если его нет или оно совпадает с основным."""
    ru = (movie['title_ru'] or '').strip()
    return '' if not ru or ru.casefold() == display_title(movie).casefold() else ru


def genres(movie) -> list[str]:
    return [g for g in (movie['genres_ru'] or []) if g]


def main_genre(movie) -> str:
    g = genres(movie)
    return g[0] if g else ''


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f'{n} {one}'
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return f'{n} {few}'
    return f'{n} {many}'


def plural_films(n: int) -> str:
    return _plural(n, 'фильм', 'фильма', 'фильмов')


def plural_sessions(n: int) -> str:
    return _plural(n, 'сеанс', 'сеанса', 'сеансов')


def sort_films(films: list[dict]) -> list[dict]:
    """Алфавит по показываемому названию (без учёта регистра), при равенстве — по id: порядок один и тот же у списка и карусели."""
    return sorted(films, key=lambda f: (display_title(f).casefold(), f['id']))


def page_count(total: int, page_size: int) -> int:
    return max(1, -(-total // page_size))


def clamp_page(page: int, pages: int) -> int:
    return page if 1 <= page <= pages else 1


def page_of(index: int, page_size: int) -> int:
    """Страница списка (с 1), на которой лежит фильм с индексом index."""
    return index // page_size + 1


def list_line(movie, title_max: int) -> str:
    """`🎬 Название · жанр · 7 сеансов` (жанра нет — без него)."""
    parts = [f"🎬 {truncate(display_title(movie), title_max)}"]
    g = main_genre(movie)
    if g:
        parts.append(g.lower())
    parts.append(plural_sessions(movie['sessions']))
    return ' · '.join(parts)


def list_head(day: date, today: date, total: int, page: int, pages: int) -> str:
    head = f'🎬 Киноафиша · {format_day_title(day, today)} · {plural_films(total)}'
    return head + (f' · стр. {page}/{pages}' if pages > 1 else '')


def empty_text(day: date, today: date) -> str:
    if day == today:
        return 'На сегодня сеансов нет 🤷'
    if day == today + timedelta(days=1):
        return 'На завтра сеансов нет 🤷'
    return f'На {format_day_title(day, today)} сеансов нет 🤷'


def day_bounds(day: date, now: datetime | None = None) -> tuple[datetime, datetime]:
    """[начало, конец) дня по Белграду; сегодня — от текущего момента (прошедшие сеансы не показываем)."""
    start = datetime.combine(day, time.min, tzinfo=BELGRADE_TZ)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=BELGRADE_TZ)
    now = now or datetime.now(BELGRADE_TZ)
    if start < now < end:
        start = now
    elif now >= end:
        start = end                       # день прошёл целиком: пустой интервал
    return start, end


def _description_source(movie) -> str:
    text = (movie['description_ru'] or '').strip() or (movie['description_src'] or '').strip()
    return _BLANKS.sub('\n\n', text.replace('\r', ''))


def preview_text(movie) -> str:
    text = _description_source(movie)
    if len(text) <= PREVIEW_CHARS:
        return text
    return truncate_word_boundary(text, PREVIEW_CHARS)


def full_text(movie) -> str:
    text = _description_source(movie)
    return text if len(text) <= FULL_MAX_CHARS else truncate_word_boundary(text, FULL_MAX_CHARS)


def has_more_description(movie) -> bool:
    """Кнопка «Полное описание» нужна, только если полное длиннее превью."""
    return len(_description_source(movie)) > len(preview_text(movie).removesuffix('...'))


def title_block(movie) -> str:
    """HTML: название, второй строкой русское, жанры."""
    lines = [f'🎬 <b>{escape(display_title(movie))}</b>']
    ru = ru_title(movie)
    if ru:
        lines.append(escape(ru))
    g = genres(movie)
    if g:
        lines.append('🎭 ' + escape(', '.join([g[0].capitalize()] + g[1:])))
    return '\n'.join(lines)


def card_text(movie) -> str:
    text = title_block(movie)
    preview = preview_text(movie)
    if preview:
        text += '\n\n' + escape(preview)
    return text


def description_screen_text(movie) -> str:
    body = full_text(movie) or 'Описание недоступно.'
    return f'{title_block(movie)}\n\n{escape(body)}'


def session_label(showtime: datetime, cinema_name: str) -> str:
    return f"{showtime.astimezone(BELGRADE_TZ):%H:%M} — {cinema_name}"
