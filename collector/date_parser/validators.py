from datetime import datetime, date


def is_valid_date(day: int, month: int, year: int, ref_year: int | None = None) -> bool:

    current_year = ref_year if ref_year is not None else datetime.now().year

    if not (1 <= month <= 12):
        return False

    if not (current_year - 1 <= year <= current_year + 2):
        return False

    try:
        date(year, month, day)
        return True
    except ValueError:
        return False


def is_valid_month_num(month: int) -> bool:
    return 1 <= month <= 12


def is_valid_day_num(day: int) -> bool:
    return 1 <= day <= 31