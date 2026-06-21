import re
from datetime import date, datetime
from loguru import logger

from collector.date_parser.validators import is_valid_date
from collector.date_parser.utils import (
    parse_day_of_week,
    parse_relative_date,
)

MONTH_MAP: dict[str, int] = {
    'январь': 1, 'января': 1, 'янв': 1, 'january': 1, 'jan': 1,
    'февраль': 2, 'февраля': 2, 'фев': 2, 'february': 2, 'feb': 2,
    'март': 3, 'марта': 3, 'мар': 3, 'march': 3, 'mar': 3,
    'апрель': 4, 'апреля': 4, 'апр': 4, 'april': 4, 'apr': 4,
    'май': 5, 'мая': 5, 'may': 5,
    'июнь': 6, 'июня': 6, 'июн': 6, 'june': 6, 'jun': 6,
    'июль': 7, 'июля': 7, 'июл': 7, 'july': 7, 'jul': 7,
    'август': 8, 'августа': 8, 'авг': 8, 'august': 8, 'aug': 8,
    'сентябрь': 9, 'сентября': 9, 'сен': 9, 'september': 9, 'sep': 9,
    'октябрь': 10, 'октября': 10, 'окт': 10, 'october': 10, 'oct': 10,
    'ноябрь': 11, 'ноября': 11, 'ноя': 11, 'november': 11, 'nov': 11,
    'декабрь': 12, 'декабря': 12, 'дек': 12, 'december': 12, 'dec': 12,
}

DAY_ABBR = (
    'пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс',
    'пон', 'вторник', 'среда', 'четверг', 'пятница', 'суббота', 'воскресенье',
    'mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun',
    'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday',
)

_P1 = re.compile(
    r'(?<!\d)(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{2,4})(?!\d)'
)

_P2_SHORT = re.compile(
    r'(?<!\d)(\d{1,2})[.\-/](\d{1,2})(?![.\-/\d:])'
)

_MONTHS_PATTERN = '|'.join(sorted(MONTH_MAP.keys(), key=len, reverse=True))
_P3 = re.compile(
    rf'(?<!\d)(\d{{1,2}})\s+({_MONTHS_PATTERN})(?:\s+(\d{{4}}))?',
    re.IGNORECASE
)

_P4 = re.compile(
    rf'({_MONTHS_PATTERN})\s+(\d{{1,2}})(?:[,\s]+(\d{{4}}))?',
    re.IGNORECASE
)

_P4B = re.compile(
    rf'(?<!\d)(\d{{1,2}})\s+of\s+({_MONTHS_PATTERN})(?:[,\s]+(\d{{4}}))?',
    re.IGNORECASE
)

_DAY_ABBR_PATTERN = '|'.join(sorted(DAY_ABBR, key=len, reverse=True))
_P5 = re.compile(
    rf'(?:{_DAY_ABBR_PATTERN})\.?,?\s+(\d{{1,2}})[.\-/](\d{{1,2}})(?:[.\-/](\d{{2,4}}))?',
    re.IGNORECASE
)

_P6 = re.compile(
    rf'(?:{_DAY_ABBR_PATTERN})[,.]?\s+({_MONTHS_PATTERN})\s+(\d{{1,2}})(?:[,\s]+(\d{{4}}))?',
    re.IGNORECASE
)


def _normalize_year(
    year_str: str | None,
    current_year: int,
    published_year: int | None = None,
) -> int:

    if not year_str:
        if published_year and published_year < current_year:
            return published_year
        return current_year
    year = int(year_str)
    if year < 100:
        year += 2000
    return year


def parse_date(text: str, published_at: date) -> date | None:
    """

    Иерархия приоритетов:
        1. Полный числовой:      4.05.2026
        2. Короткий числовой:    26.04 (без года)
        3. Текстовый месяц:      4 мая 2026
        4. Английский обратный:  March 27
        5. День недели + дата:   Вс., 26.04
        6. День недели + месяц:  Friday, March 27
        7. День недели:          в субботу
        8. Относительные:        завтра/сегодня
    """
    current_year = datetime.now().year
    pub_year = published_at.year

    for match in _P1.finditer(text):
        day   = int(match.group(1))
        month = int(match.group(2))
        year  = _normalize_year(match.group(3), current_year, pub_year)

        if is_valid_date(day, month, year):
            result = date(year, month, day)
            logger.debug(f"PARSED P1 | date={result} | text={text[:60]!r}")
            return result
        else:
            logger.debug(
                f"P1 match invalid: {day}.{month}.{year} | text={text[:60]!r}"
            )

    for match in _P2_SHORT.finditer(text):
        day   = int(match.group(1))
        month = int(match.group(2))
        year  = _normalize_year(None, current_year, pub_year)

        if is_valid_date(day, month, year):
            result = date(year, month, day)
            logger.debug(f"PARSED P2_SHORT | date={result} | text={text[:60]!r}")
            return result

    match = _P3.search(text)
    if match:
        day        = int(match.group(1))
        month_name = match.group(2).lower()
        year       = _normalize_year(match.group(3), current_year, pub_year)
        month      = MONTH_MAP.get(month_name)

        if month and is_valid_date(day, month, year):
            result = date(year, month, day)
            logger.debug(f"PARSED P3 | date={result} | text={text[:60]!r}")
            return result

    match = _P4B.search(text)
    if match:
        day = int(match.group(1))
        month_name = match.group(2).lower()
        year = _normalize_year(match.group(3), current_year, pub_year)
        month = MONTH_MAP.get(month_name)

        if month and is_valid_date(day, month, year):
            result = date(year, month, day)
            logger.debug(f"PARSED P4B | date={result} | text={text[:60]!r}")
            return result

    match = _P4.search(text)
    if match:
        month_name = match.group(1).lower()
        day        = int(match.group(2))
        year       = _normalize_year(match.group(3), current_year, pub_year)
        month      = MONTH_MAP.get(month_name)

        if month and is_valid_date(day, month, year):
            result = date(year, month, day)
            logger.debug(f"PARSED P4 | date={result} | text={text[:60]!r}")
            return result

    match = _P5.search(text)
    if match:
        day   = int(match.group(1))
        month = int(match.group(2))
        year  = _normalize_year(match.group(3), current_year, pub_year)

        if is_valid_date(day, month, year):
            result = date(year, month, day)
            logger.debug(f"PARSED P5 | date={result} | text={text[:60]!r}")
            return result

    match = _P6.search(text)
    if match:
        month_name = match.group(1).lower()
        day        = int(match.group(2))
        year       = _normalize_year(match.group(3), current_year, pub_year)
        month      = MONTH_MAP.get(month_name)

        if month and is_valid_date(day, month, year):
            result = date(year, month, day)
            logger.debug(f"PARSED P6 | date={result} | text={text[:60]!r}")
            return result

    result = parse_day_of_week(text, published_at)
    if result:
        logger.info(
            f"PARSED_UNCERTAIN | pattern=day_of_week | "
            f"date={result} | published_at={published_at} | "
            f"text={text[:60]!r}"
        )
        return result

    result = parse_relative_date(text, published_at)
    if result:
        logger.info(
            f"PARSED_UNCERTAIN | pattern=relative | "
            f"date={result} | published_at={published_at} | "
            f"text={text[:60]!r}"
        )
        return result

    logger.warning(f"UNPARSED | text={text[:80]!r}")
    return None