from datetime import date
from unittest.mock import MagicMock
from collector.parsers.zarko_tusic import ZarkoTusicParser


PUBLISHED = date(2026, 4, 28)  # понедельник


def make_message(text: str, entities: list = None):
    from datetime import datetime, timezone
    msg = MagicMock()
    msg.message = text
    msg.id = 999
    msg.entities = entities or []
    msg.date = datetime(2026, 4, 28, 12, 0, 0, tzinfo=timezone.utc)
    msg.photo = None
    return msg


DIGEST_TEXT = """Cобытия и тусовки на неделе: 29 апреля – 5 мая

Понедельник, 29 апреля
- Событие А / Место А, 20:00
- Событие Б / Место Б, 21:00

Вторник, 30 апреля
- Событие В / Место В, 19:00

Среда, 1 мая
- Событие Г / Место Г, 18:00
- Событие Д / Место Д, 22:00
- Событие Е / Место Е, 23:00"""


class TestZarkoTusicParser:

    def setup_method(self):
        self.parser = ZarkoTusicParser()

    def test_is_digest_true(self):
        msg = make_message(DIGEST_TEXT)
        assert self.parser._is_digest(DIGEST_TEXT) is True

    def test_is_digest_false(self):
        msg = make_message("Просто анонс без заголовков дней")
        assert self.parser._is_digest("Просто анонс без заголовков дней") is False

    def test_total_events_count(self):
        msg = make_message(DIGEST_TEXT)
        events = self.parser.parse(msg)
        assert len(events) == 6

    def test_positions_sequential(self):
        msg = make_message(DIGEST_TEXT)
        events = self.parser.parse(msg)
        positions = [e.position for e in events]
        assert positions == [1, 2, 3, 4, 5, 6]

    def test_dates_correct(self):
        msg = make_message(DIGEST_TEXT)
        events = self.parser.parse(msg)
        assert events[0].date == date(2026, 4, 29)
        assert events[1].date == date(2026, 4, 29)
        assert events[2].date == date(2026, 4, 30)
        assert events[3].date == date(2026, 5, 1)
        assert events[4].date == date(2026, 5, 1)
        assert events[5].date == date(2026, 5, 1)

    def test_event_text_contains_header(self):
        msg = make_message(DIGEST_TEXT)
        events = self.parser.parse(msg)
        assert 'Понедельник, 29 апреля' in events[0].text
        assert 'Вторник, 30 апреля' in events[2].text
        assert 'Среда, 1 мая' in events[3].text

    def test_event_text_contains_bullet(self):
        msg = make_message(DIGEST_TEXT)
        events = self.parser.parse(msg)
        assert 'Событие А' in events[0].text
        assert 'Событие Б' in events[1].text

    def test_non_digest_falls_back_to_default(self):
        text = "26 мая в Доме молодёжи Белграда — Блэйн Рейнингер"
        msg = make_message(text)
        events = self.parser.parse(msg)
        assert len(events) == 1
        assert events[0].position == 0  # DefaultParser возвращает position=0

    def test_real_digest_sample(self):
        text = """События и тусовки на неделе: 4 — 10 мая

Понедельник, 4 мая
- Sirenia / Zappa Barka, 20:00
- Suplexx, Majak / Drugstore, 20:00

Пятница, 8 мая
- Agent Steel / Fest, 20:00"""

        msg = make_message(text)
        events = self.parser.parse(msg)
        assert len(events) == 3
        assert events[0].date == date(2026, 5, 4)
        assert events[2].date == date(2026, 5, 8)

    def test_subbota_spelling(self):
        text = """Cобытия на неделе

Субота, 10 мая
- Концерт / Место, 20:00"""

        msg = make_message(text)
        events = self.parser.parse(msg)
        assert len(events) == 1
        assert events[0].date == date(2026, 5, 10)