from event_collector.scrapers.gigstix import _basis_for


def test_short_description_trusts_site_category():
    """Клубная ночь с описанием «Название | дата | клуб» — AI не пересматривает категорию сайта."""
    assert _basis_for(["Концерты"], "Karma Party | 11.12. | Zappa Baza | 22:00h") == "site"
    assert _basis_for(["Концерты"], None) == "site"
    assert _basis_for([], None) == "none"


def test_long_description_keeps_ai_check_for_broad_categories():
    text = "Балетная постановка в двух актах. " * 5
    assert _basis_for(["Концерты"], text) == "aggregate"
    assert _basis_for(["Кино/Театр"], text) == "aggregate"
    assert _basis_for(["Игры и развлечения"], text) == "site"
