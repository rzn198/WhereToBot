from datetime import datetime, timedelta

from event_collector.normalize import BELGRADE_TZ

from event_collector.scrapers.ticketsrs import TicketsRsScraper


def build(cat):
    ev = {"Title": "Событие", "Slug": "event/x_1", "StartDate": (datetime.now() + timedelta(days=5)).strftime("%Y-%m-%d"),
          "DateTime": "20:00", "Venue": "Клуб"}
    return TicketsRsScraper()._build_item(ev, cat, datetime.now(BELGRADE_TZ) - timedelta(days=1))


def test_aggregate_categories_are_unreliable():
    for cat in ("Концерты", "Кино/Театр", "Фестивали", "Спорт/активный отдых", "Мастер-классы/Нетворкинг"):
        assert build(cat).category_basis == "aggregate"


def test_refined_and_museums_are_reliable():
    for cat in ("Семейные/детские", "Игры и развлечения", "Поездки и экскурсии", "Выставки/Ярмарки"):
        assert build(cat).category_basis == "site"
