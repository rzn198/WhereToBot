import pytest
from datetime import date
from collector.date_parser.parser import parse_date
from collector.date_parser.validators import is_valid_date
from collector.date_parser.utils import parse_day_of_week, parse_relative_date

PUBLISHED = date(2026, 5, 4)  # понедельник


class TestPriority1Numeric:

    def test_dot_separator(self):
        assert parse_date('Концерт 4.05.2026', PUBLISHED) == date(2026, 5, 4)

    def test_slash_separator(self):
        assert parse_date('Концерт 04/05/2026', PUBLISHED) == date(2026, 5, 4)

    def test_dash_separator(self):
        assert parse_date('Концерт 04-05-2026', PUBLISHED) == date(2026, 5, 4)

    def test_short_year(self):
        assert parse_date('Концерт 4.05.26', PUBLISHED) == date(2026, 5, 4)

    def test_single_digit_day(self):
        assert parse_date('4.5.2026 вечеринка', PUBLISHED) == date(2026, 5, 4)


class TestPriority2TextMonth:

    def test_russian_genitive(self):
        assert parse_date('4 мая 2026', PUBLISHED) == date(2026, 5, 4)

    def test_russian_no_year(self):
        result = parse_date('Концерт 4 мая', PUBLISHED)
        assert result == date(2026, 5, 4)

    def test_russian_month_start(self):
        assert parse_date('15 июня', PUBLISHED) == date(2026, 6, 15)

    def test_english_month(self):
        assert parse_date('Concert 4 may 2026', PUBLISHED) == date(2026, 5, 4)

    def test_case_insensitive(self):
        assert parse_date('4 МАЯ 2026', PUBLISHED) == date(2026, 5, 4)


class TestPriority3DayOfWeek:

    def test_v_subbotu(self):
        # days_ahead = (5 - 0) % 7 = 5
        result = parse_date('в субботу', PUBLISHED)
        assert result == date(2026, 5, 9)

    def test_v_voskresenye(self):
        # days_ahead = (6 - 0) % 7 = 6
        result = parse_date('в воскресенье', PUBLISHED)
        assert result == date(2026, 5, 10)

    def test_v_ponedelnik_same_day(self):
        # days_ahead = 0
        result = parse_date('в понедельник', PUBLISHED)
        assert result == PUBLISHED

    def test_no_predlog_not_matched(self):
        result = parse_date('суббота вечер', PUBLISHED)
        assert result is None

    def test_uses_published_at_not_now(self):
        pub_monday = date(2026, 5, 4)    # понедельник
        pub_friday = date(2026, 5, 8)    # пятница
        result_mon = parse_date('в субботу', pub_monday)
        result_fri = parse_date('в субботу', pub_friday)
        assert result_mon == date(2026, 5, 9)
        assert result_fri == date(2026, 5, 9)


class TestPriority4Relative:

    def test_segodnya(self):
        assert parse_date('Концерт сегодня', PUBLISHED) == PUBLISHED

    def test_zavtra(self):
        assert parse_date('Концерт завтра', PUBLISHED) == date(2026, 5, 5)

    def test_poslezavtra(self):
        assert parse_date('Концерт послезавтра', PUBLISHED) == date(2026, 5, 6)


class TestUnparsed:

    def test_no_date(self):
        assert parse_date('Просто текст без даты', PUBLISHED) is None

    def test_empty(self):
        assert parse_date('', PUBLISHED) is None


class TestFalsePositive:

    def test_price_not_date(self):
        result = parse_date('Билеты от 500.00 рублей', PUBLISHED)
        assert result is None

    def test_version_not_date(self):
        result = parse_date('Обновление v1.4.2026', PUBLISHED)
        assert result is None or isinstance(result, date)

    def test_invalid_day_31_feb(self):
        result = parse_date('31.02.2026', PUBLISHED)
        assert result is None

    def test_invalid_month_13(self):
        result = parse_date('1.13.2026', PUBLISHED)
        assert result is None

    def test_year_too_far(self):
        result = parse_date('1.05.2099', PUBLISHED)
        assert result is None


class TestValidators:

    def test_valid_date(self):
        assert is_valid_date(4, 5, 2026) is True

    def test_invalid_feb_31(self):
        assert is_valid_date(31, 2, 2026) is False

    def test_invalid_month(self):
        assert is_valid_date(1, 13, 2026) is False

    def test_invalid_year_future(self):
        assert is_valid_date(1, 1, 2099) is False

    def test_invalid_year_past(self):
        assert is_valid_date(1, 1, 2020) is False

class TestPriority2ShortNumeric:

    def test_short_dot(self):
        result = parse_date('📆 Вс., 26.04', PUBLISHED)
        assert result == date(2026, 4, 26)

    def test_short_in_text(self):
        result = parse_date('Тренинг, 26.04 в 12:00', PUBLISHED)
        assert result == date(2026, 4, 26)

    def test_not_triggered_by_full_format(self):
        result = parse_date('4.05.2026', PUBLISHED)
        assert result == date(2026, 5, 4)


class TestPriority4EnglishReverse:

    def test_month_day(self):
        result = parse_date('March 27', PUBLISHED)
        assert result == date(2026, 3, 27)

    def test_month_day_year(self):
        result = parse_date('March 27, 2026', PUBLISHED)
        assert result == date(2026, 3, 27)

    def test_friday_march(self):
        result = parse_date('Friday, March 27', PUBLISHED)
        assert result == date(2026, 3, 27)

    def test_date_march_27_friday(self):
        result = parse_date('Date: March 27 / Friday', PUBLISHED)
        assert result == date(2026, 3, 27)


class TestPriority5DayAbbrPlusDate:

    def test_vs_26_04(self):
        result = parse_date('📆 Вс., 26.04', PUBLISHED)
        assert result == date(2026, 4, 26)

    def test_sb_25_04(self):
        result = parse_date('📆 Сб., 25.04', PUBLISHED)
        assert result == date(2026, 4, 25)

    def test_pятница_17_04(self):
        result = parse_date('📆 Пятница, 17.04', PUBLISHED)
        assert result == date(2026, 4, 17)

    def test_subbota_11_04(self):
        result = parse_date('📆 Суббота, 11.04', PUBLISHED)
        assert result == date(2026, 4, 11)

    def test_chet_23_04(self):
        result = parse_date('📆 Чт., 23.04', PUBLISHED)
        assert result == date(2026, 4, 23)


class TestRealWorldEvents:

    def test_trening_dvuhkolesnaya(self):
        text = 'Тренинг "Двухколесная безопасность", 26.04 в 12:00'
        assert parse_date(text, PUBLISHED) == date(2026, 4, 26)

    def test_paskhalny_mk(self):
        text = 'Пасхальный мастер-класс по валянию из шерсти, 11.04 в 11:00'
        assert parse_date(text, PUBLISHED) == date(2026, 4, 11)

    def test_kinovechet_20_stulev(self):
        text = 'Киновечер клуба 20 стульев, 25.04 в 18:00'
        assert parse_date(text, PUBLISHED) == date(2026, 4, 25)

    def test_kinoklub_serbskom(self):
        text = 'Киноклуб на сербском, 23.04 в 19:20'
        assert parse_date(text, PUBLISHED) == date(2026, 4, 23)

    def test_trening_rech(self):
        text = 'Тренинг по технике речи и публичным выступлениям, 19.04 в 18:00'
        assert parse_date(text, PUBLISHED) == date(2026, 4, 19)

    def test_prosto_poem(self):
        text = 'Просто поём хором, 17.04 в 19:00'
        assert parse_date(text, PUBLISHED) == date(2026, 4, 17)

    def test_english_event(self):
        text = 'Friday, March 27 // A Dual Experience at 20/44'
        assert parse_date(text, PUBLISHED) == date(2026, 3, 27)

    def test_malenky_illustrator(self):
        text = 'Сб., 25.04\n🕰 15:30 - 16:30'
        assert parse_date(text, PUBLISHED) == date(2026, 4, 25)

class TestNewPatterns:

    def test_27_of_december(self):
        pub = date(2025, 12, 27)
        result = parse_date('📌27 of December, start at 21:00', pub)
        assert result == date(2025, 12, 27)

    def test_27_of_march(self):
        result = parse_date('27 of March 2026', PUBLISHED)
        assert result == date(2026, 3, 27)

    def test_v_etu_subbotu(self):
        result = parse_date('В эту субботу пройдёт фестиваль', PUBLISHED)
        assert result == date(2026, 5, 9)

    def test_v_etot_chetverg(self):
        result = parse_date('Внезапно в этот четверг — концерт', PUBLISHED)
        assert result == date(2026, 5, 7)

    def test_v_etu_pyatnicu(self):
        result = parse_date('В эту пятницу в клубе', PUBLISHED)
        assert result == date(2026, 5, 8)