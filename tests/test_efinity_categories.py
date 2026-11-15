"""efinityrs: категории из ВСЕХ категорий/подкатегорий события (а не из первой)."""
from event_collector.scrapers.efinityrs import EfinityRsScraper

CATS = [
    {"Id": 1, "NameEN": "CONCERTS", "Subcategories": [{"Id": 11, "NameEN": "POP & ROCK"}, {"Id": 12, "NameEN": "HIP-HOP/RAP"}]},
    {"Id": 2, "NameEN": "THEATRE", "Subcategories": [{"Id": 21, "NameEN": "COMEDY"}, {"Id": 22, "NameEN": "MONODRAMA"}]},
    {"Id": 3, "NameEN": "FESTIVALS", "Subcategories": [{"Id": 31, "NameEN": "MUSIC"}, {"Id": 32, "NameEN": "THEATRICAL PERFORMANCES"}]},
    {"Id": 4, "NameEN": "ZAPPA", "Subcategories": [{"Id": 41, "NameEN": "ZAPPA"}]},
    {"Id": 5, "NameEN": "SAVA CENTAR", "Subcategories": [{"Id": 51, "NameEN": "SAVA CENTAR BLUE HALL"}]},
    {"Id": 6, "NameEN": "SPORT", "Subcategories": [{"Id": 61, "NameEN": "ICE HOCKEY"}]},
    {"Id": 7, "NameEN": "MUSEUM", "Subcategories": [{"Id": 71, "NameEN": "PALATA NAUKE"}]},
    {"Id": 8, "NameEN": "NEW YEAR 2027", "Subcategories": [{"Id": 81, "NameEN": "NEW YEAR 2027"}]},
]


def resolve(cats=(), subs=(), what="", url=""):
    return EfinityRsScraper().resolve_event_types(
        {"Categories": list(cats), "Subcategories": list(subs), "What": what, "UrlName": url}, CATS)


def test_monodrama_is_theatre_not_concert():
    assert resolve([2], [22]) == (["Кино/Театр"], "site", "THEATRE / MONODRAMA")


def test_festival_concerts_with_fest_in_title_or_url_get_both_labels():
    types, basis, hint = resolve([3, 1], [31, 11], what="WAKE UP FESTIVAL", url="x")
    assert types == ["Фестивали", "Концерты"] and basis == "site" and "FESTIVALS" in hint
    assert resolve([3, 1], [31, 11], what="BURAK YETER", url="wake_up_festival_burak_yeter")[0] == ["Фестивали", "Концерты"]


def test_festivals_tag_on_plain_concert_is_dropped_and_sent_to_ai():
    # общий пакет тегов FESTIVALS / CONCERTS / MUSIC / POP & ROCK на сольном концерте
    types, basis, hint = resolve([3, 1], [31, 11], what="SEKA ALEKSIĆ", url="seka_aleksic_1")
    assert types == ["Концерты"] and basis == "aggregate" and "FESTIVALS" in hint


def test_festival_as_only_category_is_kept():
    assert resolve([3], [31], what="Neki događaj") == (["Фестивали"], "site", "FESTIVALS / MUSIC")


def test_festival_theatrical_performances_without_fest_in_name_is_theatre_for_ai():
    types, basis, _ = resolve([3], [32], what="POZORIŠNA JESEN")
    assert (types, basis) == (["Кино/Театр"], "aggregate")
    assert resolve([3], [32], what="Theatre Fest")[0] == ["Фестивали", "Кино/Театр"]


def test_venue_category_does_not_hide_real_type():
    assert resolve([4, 1], [41, 12])[0] == ["Концерты"]          # ZAPPA + CONCERTS (MOSKRIDAN)
    assert resolve([5, 2], [51, 21])[0] == ["Кино/Театр"]        # Sava Centar + THEATRE/COMEDY


def test_unknown_subcategory_falls_back_to_parent():
    assert resolve([6], [61])[0] == ["Спорт/активный отдых"]
    assert resolve([7], [71])[0] == ["Выставки/Ярмарки"]
    assert resolve([1], [12])[0] == ["Концерты"]


def test_only_venue_or_new_year_is_default_concert_and_marked():
    for cats, subs in (([5], [51]), ([8], [81]), ([4], [41])):
        types, basis, _ = resolve(cats, subs)
        assert types == ["Концерты"] and basis == "default"


def test_event_without_categories_is_default():
    assert resolve() == (["Концерты"], "default", None)


def test_comedy_and_theatre_do_not_duplicate_label():
    assert resolve([2], [21])[0] == ["Кино/Театр"]
