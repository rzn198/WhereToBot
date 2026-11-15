"""gigstix: полное описание события берётся из секции «O događaju», а не из строки заголовка."""
from selectolax.parser import HTMLParser

from event_collector.scrapers.gigstix import GigstixScraper

LONG = ("U petak, 20. novembra, ØOMO se vraća u Hangar sa dva izuzetna imena savremene elektronske muzike - "
        "Avalon Emerson i Young Marco.")
LONG2 = "Ovoga puta muzika se seli u samo središte prostora. U posebnom 360° formatu, DJ pult biće usred dancefloora."


def page(about_title="O događaju", extra=""):
    return f"""
<html><head><title>FOMO pres. Avalon Emerson - GigsTix</title></head><body>
<div class="gt-page-content"><div class="gt-content">FOMO pres. Avalon Emerson and Young Marco | 20.11. | Hangar &#8211; Luka Beograd</div></div>
<div class="gt-event-sections">
  <div class="gt-section" data-gt-type="content"><div class="gt-section-title">{about_title}</div>
     <div class="gt-content"><p>{LONG}</p> <p>{LONG2}</p></div></div>
  <div class="gt-section" data-gt-type="content"><div class="gt-section-title">Ulaznice</div>
     <div class="gt-content">REGULARCENA: 1.990 DINVIPCENA: 3.390 DINKupi ulaznice onlajn</div></div>
  {extra}
</div></body></html>"""


def extract(html):
    return GigstixScraper()._extract_description(HTMLParser(html))


def test_description_is_taken_from_about_section_not_from_title_block():
    d = extract(page())
    assert d is not None and d.startswith("U petak, 20. novembra") and LONG2 in d
    assert "Hangar – Luka Beograd" not in d and "REGULAR" not in d


def test_unknown_section_title_falls_back_to_longest_content_block():
    d = extract(page(about_title="Nešto drugo"))
    assert d is not None and LONG in d and "REGULAR" not in d


def test_about_title_variants_are_recognised():
    for title in ("O događaju", "O dogadjaju", "About the event", "Opis"):
        assert extract(page(about_title=title)).startswith("U petak"), title


def test_old_layout_without_sections_still_works():
    html = '<html><body><div class="gt-page-content"><div class="gt-content"><p>Staro opis</p></div></div></body></html>'
    assert extract(html) == "Staro opis"


def test_page_without_description_gives_none():
    assert extract("<html><body><p>nista</p></body></html>") is None


def test_broad_gigstix_categories_are_checked_by_ai():
    from event_collector.scrapers.gigstix import _basis
    assert _basis([]) == 'none'
    assert _basis(["Концерты"]) == 'aggregate'
    assert _basis(["Кино/Театр"]) == 'aggregate'
    assert _basis(["Игры и развлечения"]) == 'site'
    assert _basis(["Фестивали", "Концерты"]) == 'aggregate'
