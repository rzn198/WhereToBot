"""Метка «Музеи»: только точное название целиком у двух источников; скрытия нет; AI и словарь метку выдать не могут."""
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from collector.categorizer_v2 import category_names
from event_collector.ai_categories import EventCategoryAI
from event_collector.normalize import match_phrase, match_whole, report_rule_hits
from event_collector.scrapers import polako, ticketsrs
from event_collector.scrapers.base import EventItem
from shared.categories import MUSEUMS

TICKETS_MUSEUMS = ["Konak kneginje Ljubice", "Muzej Jovana Cvijića", "Muzej Ive Andrića"]
NOW = datetime(2026, 11, 11, tzinfo=timezone.utc)


def ticket_item(title, category="Выставки/Ярмарки"):
    ev = {"Title": title, "Slug": "event/x_1", "StartDate": "2026-12-01", "DateTime": "2026-12-01 10:00", "Venue": "v"}
    return ticketsrs.TicketsRsScraper()._build_item(ev, category, NOW)


@pytest.mark.parametrize("title", TICKETS_MUSEUMS + ["MUZEJ IVE ANDRICA", "muzej  jovana cvijića.", "Konak Kneginje Ljubice!"])
def test_ticketsrs_museum_label(title):
    it = ticket_item(title)
    assert it.event_types == [MUSEUMS] and it.category_basis == 'fixed' and it.label_rule
    assert not it.auto_hide_reason and it.category_hint is None


@pytest.mark.parametrize("title", [
    "Muzej Ive Andrića — specijalna tura", "Noć muzeja: Muzej Ive Andrića", "Spomen-muzej Ive Andrića",
    "Muzej Ive Andrića i Muzej Jovana Cvijića", "Konak kneginje Ljubice: koncert", "Muzej Nikole Tesle", "Koncert", "Muzej"])
def test_ticketsrs_no_label_for_other_titles(title):
    it = ticket_item(title)
    assert it.event_types == ["Выставки/Ярмарки"] and it.label_rule is None and it.category_basis == 'site'


def test_polako_museum_rule_whole_title():
    assert polako.museum_rule("Музей Паранормального") == "Музей Паранормального"
    assert polako.museum_rule("музей  паранормального!") == "Музей Паранормального"
    for t in ("Музей Паранормального — Хэллоуин", "Экскурсия в Музей Паранормального", "Музей Николы Теслы",
              "Традиционные украшения Балкан (из коллекции Muzeja primenjene umetnosti)",
              "Национальный музей — бесплатный вход", "Исторический музей — постоянная экспозиция", ""):
        assert polako.museum_rule(t) is None


def test_museum_rule_does_not_hide():
    # метка и автоскрытие независимы: музей не скрывается, AA не получает метку
    assert polako.auto_hide_reason("Музей Паранормального") is None
    assert polako.museum_rule("Анонимные Алкоголики — онлайн-встреча") is None


def test_match_helpers_whole_vs_phrase():
    assert match_whole("Muzej Ive Andrića", ["Muzej Ive Andrića"])
    assert match_whole("Muzej Ive Andrića tura", ["Muzej Ive Andrića"]) is None
    assert match_phrase("Muzej Ive Andrića tura", ["Muzej Ive Andrića"]) == "Muzej Ive Andrića"


def test_museums_label_is_not_a_known_category():
    """Метку не могут выдать ни AI (белый список = ключи categorizers_v2.yaml), ни словарь, ни прочие скрейперы."""
    assert MUSEUMS not in category_names()


def test_ai_skips_label_rule_items():
    ai = EventCategoryAI(cfg={'method': 'ai', 'scope': 'all'}, client=MagicMock())
    ai._init()
    plain = EventItem(source='ticketsrs', source_url='u1', title='t', starts_at=NOW, event_types=['Концерты'], category_basis='aggregate')
    museum = EventItem(source='ticketsrs', source_url='u2', title='Muzej Ive Andrića', starts_at=NOW, event_types=[MUSEUMS],
                       category_basis='fixed', label_rule='Muzej Ive Andrića')
    assert ai._candidates([plain, museum]) == [plain]
    ai.scope = 'unreliable'
    assert ai._candidates([plain, museum]) == [plain]


def test_report_rule_hits_warns_when_rule_unmatched(capsys):
    from loguru import logger
    msgs = []
    sink = logger.add(lambda m: msgs.append(str(m)), level="INFO")
    try:
        report_rule_hits('ticketsrs', 'museum', TICKETS_MUSEUMS, ["Muzej Ive Andrića", "Muzej Ive Andrića", None])
    finally:
        logger.remove(sink)
    text = "\n".join(msgs)
    assert "matched=2" in text
    assert text.count("AUTORULE_NO_MATCH") == 2 and "Konak kneginje Ljubice" in text and "Muzej Jovana Cvijića" in text


# ── Postgres: сохранение и разовый скрипт ─────────────────────────────────────────────────────────────────────
import os

import asyncpg

DSN = os.getenv("DEDUP_TEST_DSN")
pg = pytest.mark.skipif(not DSN, reason="нужна DEDUP_TEST_DSN")


@pytest.fixture
async def pool():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
    await p.execute("TRUNCATE events RESTART IDENTITY CASCADE")
    yield p
    await p.close()


async def _add(pool, source, url, title, venue="v", types=("Выставки/Ярмарки",), hidden=False, day="2099-12-01"):
    from datetime import date
    await pool.execute(
        "INSERT INTO events (source, source_url, title, venue, starts_at, city, event_types, hidden_by_admin) "
        "VALUES ($1,$2,$3,$4,$5,'Belgrade',$6,$7)", source, url, title, venue, date.fromisoformat(day), list(types), hidden)


@pg
async def test_saver_labels_without_hiding(pool):
    from datetime import timedelta
    from event_collector.saver import save_events
    day = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=10)   # внутри окна сохранения
    await _add(pool, 'ticketsrs', 'http://t/1', 'Muzej Ive Andrića', day=day.date().isoformat())   # лежит со старой категорией, видимая
    it = ticket_item('Muzej Ive Andrića')
    it.source_url = 'http://t/1'
    it.starts_at = day
    new = ticket_item('Muzej Jovana Cvijića')
    new.source_url, new.starts_at = 'http://t/2', day
    await save_events(pool, [it, new])
    rows = {r['source_url']: (r['event_types'], r['hidden_by_admin']) for r in
            await pool.fetch("SELECT source_url, event_types, hidden_by_admin FROM events")}
    assert rows == {'http://t/1': ([MUSEUMS], False), 'http://t/2': ([MUSEUMS], False)}


@pg
async def test_apply_source_rules_script(pool, monkeypatch, capsys):
    from scripts import apply_source_rules as sr

    async def fake_init():
        return pool

    async def fake_close(p):
        return None
    monkeypatch.setattr(sr, 'init_db_pool', fake_init)
    monkeypatch.setattr(sr, 'close_db_pool', fake_close)
    monkeypatch.setattr(sr, 'setup_logging', lambda *a, **k: None)

    await _add(pool, 'polako', 'p/aa', 'Анонимные Алкоголики — онлайн-встреча', types=('Мастер-классы/Нетворкинг',))
    await _add(pool, 'polako', 'p/mu', 'Музей Паранормального', venue='Muzej Paranormalnog', types=('Поездки и экскурсии',))
    await _add(pool, 'polako', 'p/mu2', 'Музей Паранормального — Хэллоуин', types=('Поездки и экскурсии',))
    await _add(pool, 'polako', 'p/kd', 'Сигареты и алкоголь от Kedlavi', types=('Концерты',))
    await _add(pool, 'polako', 'p/si', 'Женская группа «Сёстры»', types=('Мастер-классы/Нетворкинг',))
    for i, t in enumerate(TICKETS_MUSEUMS):
        await _add(pool, 'ticketsrs', f't/{i}', t)
    await _add(pool, 'ticketsrs', 't/x', 'Muzej Ive Andrića — specijalna tura')
    await _add(pool, 'ticketsrs', 't/same', 'Narodni muzej', venue='Narodni muzej')       # кандидат вне правил
    await _add(pool, 'biletrs', 'b/1', 'Музей Паранормального')                            # другой источник — правило не действует

    async def snap():
        return {r['source_url']: (list(r['event_types']), r['hidden_by_admin']) for r in
                await pool.fetch("SELECT source_url, event_types, hidden_by_admin FROM events")}
    before = await snap()

    assert await sr.run(apply=False, candidates=False) == 0
    assert await snap() == before                                   # просмотр ничего не меняет
    out = capsys.readouterr().out
    assert "будет скрыто 1" in out and "будет изменено 4" in out

    assert await sr.run(apply=False, candidates=True) == 0
    assert await snap() == before
    out = capsys.readouterr().out
    assert "Narodni muzej" in out and "specijalna tura" in out and "Kedlavi" not in out

    assert await sr.run(apply=True, candidates=False) == 0
    after = await snap()
    assert after['p/aa'] == (['Мастер-классы/Нетворкинг'], True)    # AA: скрыто, категории прежние
    assert after['p/mu'] == ([MUSEUMS], False)                       # музей: метка, не скрыт
    assert all(after[f't/{i}'] == ([MUSEUMS], False) for i in range(3))
    for url in ('p/mu2', 'p/kd', 'p/si', 't/x', 't/same', 'b/1'):
        assert after[url] == before[url], url                         # остальное не тронуто
