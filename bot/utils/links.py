"""Ссылки на записи: пост в канале и ссылка в бота на карточку."""
import re
from datetime import date

FAV_START_RE = re.compile(r'^fav_(\d{1,18})$')
# /start cw_<категория>_<с>_<по>_<ссылка>: карточка из окна категории; даты YYYYMMDD, ссылка 123 (пост) или e45 (событие)
WINDOW_START_RE = re.compile(r'^cw_([a-z]{2,12})_(\d{8})_(\d{8})_(e?\d{1,18})$')


# /start cm_<id фильма>_<YYYYMMDD>: карточка фильма из списка киноафиши
CINEMA_START_RE = re.compile(r'^cm_(\d{1,12})_(\d{8})$')


def post_url(username: str | None, channel_id: int | None, tg_id: int | None) -> str:
    """Оригинал поста в канале ('' — собрать ссылку не из чего)."""
    if not tg_id:
        return ''
    if username:
        return f"https://t.me/{username.lstrip('@')}/{tg_id}"
    if channel_id:
        return f"https://t.me/c/{channel_id}/{tg_id}"
    return ''


def bot_card_url(bot_username: str, surrogate_id: int) -> str:
    """Ссылка в бота: по нажатию Telegram присылает боту /start fav_<id>, бот открывает карточку в избранном."""
    return f"https://t.me/{bot_username.lstrip('@')}?start=fav_{int(surrogate_id)}"


def window_card_url(bot_username: str, code: str, first: date, last: date, ref: int | str) -> str:
    """Ссылка из окна категории: по нажатию бот получает /start cw_... и открывает эту карточку в том же списке."""
    return (f"https://t.me/{bot_username.lstrip('@')}"
            f"?start=cw_{code}_{first:%Y%m%d}_{last:%Y%m%d}_{ref}")


def parse_window_payload(raw: str | None):
    """'cw_concerts_20261120_20261122_e45' → ('concerts', date, date, 'e45' | 123); всё остальное → None."""
    m = WINDOW_START_RE.match(raw) if isinstance(raw, str) else None
    if not m:
        return None
    try:
        first = date(int(m.group(2)[:4]), int(m.group(2)[4:6]), int(m.group(2)[6:]))
        last = date(int(m.group(3)[:4]), int(m.group(3)[4:6]), int(m.group(3)[6:]))
    except ValueError:
        return None
    ref = m.group(4)
    return m.group(1), first, last, (ref if ref.startswith('e') else int(ref))


def cinema_card_url(bot_username: str, movie_id: int, day: date) -> str:
    """Ссылка из списка киноафиши: по нажатию бот получает /start cm_... и открывает карточку фильма на этот день."""
    return f"https://t.me/{bot_username.lstrip('@')}?start=cm_{int(movie_id)}_{day:%Y%m%d}"


def parse_cinema_payload(raw: str | None):
    """'cm_17_20261127' → (17, date(2026, 11, 27)); всё остальное → None."""
    m = CINEMA_START_RE.match(raw) if isinstance(raw, str) else None
    if not m:
        return None
    try:
        day = date(int(m.group(2)[:4]), int(m.group(2)[4:6]), int(m.group(2)[6:]))
    except ValueError:
        return None
    return int(m.group(1)), day
