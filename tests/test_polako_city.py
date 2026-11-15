"""Город polako: city.name площадки на сайте главнее угадывания по названию; запасные источники; Belgrade по умолчанию."""
import pytest

from event_collector.scrapers.polako import PolakoScraper, resolve_city


def loc(name=None, addr=None, city=None):
    d = {"name": name, "address_text": addr}
    if city is not None:
        d["city"] = {"id": "x", "country": "RS", "name": city}
    return d


@pytest.mark.parametrize("location,title,expected", [
    (loc("Dom Vojske Srbije", "Braće Jugovića 19, Beograd", "Belgrade"), "t", ("Belgrade", "site")),
    (loc("Pančevo Cultural Center", None, "Pancevo"), "t", ("Pančevo", "site")),          # без диакритики -> канон
    (loc("Kulturni Centar LAB", None, "Novi Sad"), "t", ("Novi Sad", "site")),
    (loc("Galerija Matice Srpske", None, "Novi Sad"), "Выставка", ("Novi Sad", "site")),   # площадка без города в названии
    (loc("Ummus caffe", None, "Kragujevac"), "TAM @ Ummus", ("Kragujevac", "site")),
    (loc("Hotel Constantine The Great", None, "Nis"), "t", ("Niš", "site")),
    (loc("Zemun kej", None, "Zemun"), "t", ("Belgrade", "site")),                          # район Белграда
    (loc("Sinagoga", "Jevrejska 11, Novi Sad", None), "t", ("Novi Sad", "address")),       # города нет, но он в адресе
    (loc("Muzej Niš", None, None), "t", ("Niš", "venue")),
    (loc("Beton", None, None), "Vivalis — музыка из Skyrim (Нови-Сад)", ("Novi Sad", "title")),
    (loc("Beton", None, None), "Просто вечер", ("Belgrade", "default")),
    ({}, "Без площадки", ("Belgrade", "default")),
    ({"city": "d26aa821-uuid"}, "t", ("Belgrade", "default")),                              # city пришёл голым id, не объект
    ({"city": {"name": None}, "name": "Klub, Subotica"}, "t", ("Subotica", "venue")),
])
def test_resolve_city(location, title, expected):
    assert resolve_city(location, title) == expected


def test_site_city_wins_over_conflicting_venue_text():
    # площадка называется «Niš…», но сайт говорит Белград: верим сайту
    assert resolve_city(loc("Niš bar", None, "Belgrade"), "t") == ("Belgrade", "site")


def test_build_item_uses_site_city():
    ev = {"id": "e1", "name": "Выставка", "start_date": "2099-01-01T18:00:00",
          "location": loc("Galerija Matice Srpske", None, "Novi Sad")}
    it = PolakoScraper()._build_item(ev)
    assert it.city == "Novi Sad"
    ev2 = {"id": "e2", "name": "Концерт", "start_date": "2099-01-01T18:00:00",
           "location": loc("Zappa Baza", "Beograd", "Belgrade")}
    assert PolakoScraper()._build_item(ev2).city == "Belgrade"


def test_api_fields_request_city_and_address():
    from event_collector.scrapers.polako import API_FIELDS
    assert "location.city.name" in API_FIELDS and "location.address_text" in API_FIELDS


from event_collector.city_normalizer import find_city_in_text, normalize_city


@pytest.mark.parametrize("venue,expected", [
    ("Svetosavski dom u Nišu", "Niš"),
    ("Dom kulture Pirot", "Pirot"),
    ("Stari Silos, Kneza Miloša 3A Jagodina", "Jagodina"),
    ("TVRĐAVA KASTEL – VELIKA SCENA, BANJA LUKA", "Banja Luka"),
    ("ARENA NOVI PARK, BANJA LUKA", "Banja Luka"),
    ("HALA ZORKA, ŠABAC", "Šabac"),
    ("SENTA, NARODNA BAŠTA", "Senta"),
    ("HALA SPORTOVA, UŽICE", "Užice"),
    ("Vivalis (Нови-Сад)", "Novi Sad"),
    ("Koncert u Novom Sadu", "Novi Sad"),
    ("Klub u Beogradu", "Belgrade"),
    ("DOM OMLADINE BEOGRADA / VELIKA SALA", "Belgrade"),
    ("NACIONALNA KLASA, UŠĆE 11", "Belgrade"),          # города нет — по умолчанию Белград
    ("Zappa Baza", "Belgrade"),
])
def test_find_city_in_text_new_aliases(venue, expected):
    assert find_city_in_text(venue) == expected


@pytest.mark.parametrize("text", ["Oni nisu došli", "Nisu svi tu", "Nisko plafon", "Senta Berger", "Prirodni muzej"])
def test_short_aliases_do_not_false_match(text):
    # «nisu»/«nisko» не дают Ниш; «Senta Berger» — имя, но слово senta по границам совпадёт: проверяем только Niš
    assert find_city_in_text(text) != "Niš"


def test_normalize_city_new_values():
    assert normalize_city("Pancevo") == "Pančevo" and normalize_city("Nis") == "Niš" and normalize_city("Стамбул") == "Istanbul"
