from datetime import date
from pathlib import Path

from event_collector.scrapers.balkan_promo import (
    BalkanPromoScraper, club_addresses, extract_description, parse_gigs,
)

HOME_HTML = (Path(__file__).parent / 'fixtures' / 'balkan_promo_home.html').read_text(encoding='utf-8')
TODAY = date(2026, 11, 14)


def items(today=TODAY):
    s = BalkanPromoScraper()
    gigs, addr = parse_gigs(HOME_HTML), club_addresses(HOME_HTML)
    return [i for i in (s._build_item(g, addr, today) for g in gigs) if i]


def test_parse_gigs_finds_all_concerts():
    assert len(parse_gigs(HOME_HTML)) == 8


def test_missing_or_broken_block_returns_none():
    assert parse_gigs('<html><body>нет данных</body></html>') is None
    assert parse_gigs('<script type="application/json" id="gigs">{oops</script>') is None


def test_cancelled_and_past_are_skipped():
    titles = [i.title for i in items()]
    assert 'TIAMAT' not in titles                       # отменён
    assert titles.count('Anacondaz') == 1               # Будва отменена, Белград остался
    assert 'НАИВ' in titles
    later = [i.title for i in items(today=date(2026, 11, 16))]
    assert 'НАИВ' not in later                          # прошёл


def test_fields_and_city():
    by_url = {i.source_url: i for i in items()}
    naiv = by_url['https://balkan-promo.com/koncerty/naiv/']
    assert naiv.starts_at.date() == date(2026, 11, 15) and naiv.venue == 'Zappa Baza'
    assert naiv.city == 'Belgrade' and naiv.event_types == ['Концерты'] and naiv.category_basis == 'fixed'
    assert naiv.images == 'https://balkan-promo.com/photos/hm/naiv-pvu7z4bk8.jpg'
    istanbul = by_url['https://balkan-promo.com/koncerty/anacondaz-in-istambul/']
    assert istanbul.city == 'Istanbul' and istanbul.venue == 'Dorock XL'
    assert all(i.source == 'balkan-promo' and i.dedup_key for i in by_url.values())


PAGES = Path(__file__).parent / 'fixtures' / 'balkan_promo_pages'


def _page(name: str) -> str:
    return (PAGES / f'{name}.html').read_text(encoding='utf-8')


def test_description_is_full_about_block_not_truncated_meta():
    d = extract_description(_page('naiv'))
    assert d.startswith('Отцы-основатели русского панк-рока')      # без префикса «НАИВ: 15 ноября 2026, Zappa Baza…»
    assert d.endswith('равнодушным не останется никто.')            # не обрезано на полуслове
    assert d.count('\n\n') == 2                                     # три абзаца
    assert 'Билет приходит' not in d and 'Скачайте' not in d       # FAQ и блок покупки не попадают


def test_description_joins_lead_and_paragraph():
    d = extract_description(_page('anacondaz-beograd'))
    assert d.startswith('Anacondaz привозят в Белград своё шоу Pauzern Carnival.\n\nPauzern Carnival это')


def test_no_about_block_means_no_description():
    # у концерта нет блока «О концерте»: в og:description только «Название: дата, клуб, город.»
    assert extract_description(_page('booze-and-glory')) is None
    assert extract_description('<html><body><main><p>Билет приходит на почту. Скачайте билет сразу, это очень важно.</p></main></body></html>') is None
