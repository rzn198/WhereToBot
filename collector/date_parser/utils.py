import re
from datetime import date, timedelta

DAY_OF_WEEK_MAP = {
    'понедельник': 0,
    'вторник':     1,
    'среду':       2,
    'четверг':     3,
    'пятницу':     4,
    'субботу':     5,
    'воскресенье': 6,
}

DAY_OF_WEEK_PATTERN = re.compile(
    r'(?:в|во)\s+(?:эту|этот)?\s*(понедельник|вторник|среду|четверг|пятницу|субботу|воскресенье)',
    re.IGNORECASE
)


def parse_day_of_week(text: str, published_at: date) -> date | None:

    match = DAY_OF_WEEK_PATTERN.search(text)
    if not match:
        return None

    day_name = match.group(1).lower()
    target_weekday = DAY_OF_WEEK_MAP.get(day_name)

    if target_weekday is None:
        return None

    pub_weekday = published_at.weekday()
    days_ahead = (target_weekday - pub_weekday) % 7

    return published_at + timedelta(days=days_ahead)


def parse_relative_date(text: str, published_at: date) -> date | None:

    text_lower = text.lower()

    if 'послезавтра' in text_lower:
        return published_at + timedelta(days=2)

    if 'завтра' in text_lower:
        return published_at + timedelta(days=1)

    if 'сегодня' in text_lower:
        return published_at

    return None