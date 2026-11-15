import pytest
from event_collector.scrapers.polako import CATEGORY_MAP, PolakoScraper
from shared.categories import normalize_category

ALLOWED = {"Концерты", "Кино/Театр", "Выставки/Ярмарки", "Фестивали", "Мастер-классы/Нетворкинг",
           "Семейные/детские", "Игры и развлечения", "Поездки и экскурсии", "Спорт/активный отдых"}


def ev(cat, name="Событие"):
    return {"id": "1", "name": name, "start_date": "2026-12-01T20:00:00", "description": "текст",
            "location": {"name": "Клуб"}, "category": ({"name": cat} if cat else None), "translations": []}


def test_map_values_are_known_categories():
    for v in CATEGORY_MAP.values():
        assert v is None or normalize_category(v) in ALLOWED


def test_site_category_used():
    it = PolakoScraper()._build_item(ev("Квиз"))
    assert it.event_types == ["Игры и развлечения"] and it.category_basis == "site" and it.category_hint == "Квиз"


def test_ambiguous_goes_to_dictionary_and_is_marked_unreliable():
    it = PolakoScraper()._build_item(ev("Шоу и перформансы", "Концерт группы"))
    assert it.category_basis in ("dict", "none")


def test_missing_category_falls_back():
    it = PolakoScraper()._build_item(ev(None, "Stand up вечер"))
    assert it.category_basis in ("dict", "none") and it.category_hint is None
