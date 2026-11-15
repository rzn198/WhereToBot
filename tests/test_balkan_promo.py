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


def test_description_prefers_specific_meta_then_paragraph():
    page = '<html><head><meta property="og:description" content="Рок, метал и панк в клубах Zappa Barka и Zappa Baza. Билет в евро."></head><body><main><p>Короткий.</p><p>' + 'НАИВ — легендарная панк-группа из Санкт-Петербурга, играет в Белграде свой единственный концерт. ' * 2 + '</p></main></body></html>'
    assert extract_description(page).startswith('НАИВ — легендарная')
    specific = '<head><meta property="og:description" content="Концерт НАИВ в клубе Zappa Baza: панк-рок из Санкт-Петербурга, 15 ноября."></head>'
    assert extract_description(specific).startswith('Концерт НАИВ')
    assert extract_description('<html><body><main><p>Билет приходит на почту. Скачайте билет сразу, это очень важно и достаточно длинно для порога.</p></main></body></html>') is None
