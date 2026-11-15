"""Автоскрытие polako: точное совпадение по целым словам, без расширений; скрытие только при вставке."""
import os
from datetime import datetime

import asyncpg
import pytest

from event_collector.saver import save_events
from event_collector.scrapers.base import EventItem
from event_collector.scrapers.polako import AUTO_HIDE_TITLE_PHRASES, auto_hide_reason

# реальные названия AA из прода (2026-11-11)
MUST_HIDE = [
    "Анонимные Алкоголики — Русскоязычное сообщество в Сербии",
    "Анонимные Алкоголики — онлайн-встреча",
    "Анонимные Алкоголики — встреча (Цой-центр, Novi Sad)",
    "Анонимные Алкоголики — встреча (Цой-центр, Нови-Сад)",
    "Анонимные Алкоголики — встреча (Resavska 28, Beograd)",
    "АНОНИМНЫЕ   алкоголики",
    "анонимные-алкоголики",
    "Anonimni Alkoholičari",
    "ANONIMNI ALKOHOLICARI — sastanak",
    "Alcoholics Anonymous (Beograd)",
]
MUST_STAY = [
    "Сигареты и алкоголь от Kedlavi - Vrtoglavica, Gornji Klub",
    "Женская группа «Сёстры» — встреча (Ресавска 28, Белград)",
    "Анонимность и алкоголь: лекция",
    "Безалкогольный фестиваль",
    "Алкоголики анонимные",           # другой порядок слов — в списке нет
    "Группа анонимных алкоголиков",   # другая форма — в списке нет
    "Анонимные алкоголи",             # обрезанное слово
    "Анонимные и алкоголики",         # слова не подряд
    "Anonymous Rave", "Anonimni", "Alcoholics", "",
]


@pytest.mark.parametrize("title", MUST_HIDE)
def test_hides_exact_phrases(title):
    assert auto_hide_reason(title) in AUTO_HIDE_TITLE_PHRASES


@pytest.mark.parametrize("title", MUST_STAY)
def test_does_not_widen(title):
    assert auto_hide_reason(title) is None


def test_none_title():
    assert auto_hide_reason(None) is None


def test_phrase_list_is_exactly_three():
    assert len(AUTO_HIDE_TITLE_PHRASES) == 3


def test_cyrillic_y_not_folded():
    # «й»≠«и»: снятие диакритики не должно расширять совпадения кириллицы
    assert auto_hide_reason("Анонимные алкоголийи") is None


DSN = os.getenv("DEDUP_TEST_DSN")
pg = pytest.mark.skipif(not DSN, reason="нужна DEDUP_TEST_DSN")


def item(url, title, hide=None):
    return EventItem(source='polako', source_url=url, title=title, starts_at=datetime(2026, 12, 1),
                     city='Belgrade', event_types=['Мастер-классы/Нетворкинг'], auto_hide_reason=hide)


@pytest.fixture
async def pool():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
    await p.execute("TRUNCATE events RESTART IDENTITY CASCADE")
    yield p
    await p.close()


@pg
async def test_insert_hidden_and_resave_keeps_admin_decision(pool):
    aa = "Анонимные Алкоголики — онлайн-встреча"
    await save_events(pool, [item("http://aa/1", aa, auto_hide_reason(aa)), item("http://ok/1", "Концерт", None)])
    rows = {r['source_url']: r['hidden_by_admin'] for r in await pool.fetch("SELECT source_url, hidden_by_admin FROM events")}
    assert rows == {"http://aa/1": True, "http://ok/1": False}

    # админ вернул запись; повторное сохранение с изменённым описанием её не скрывает заново
    await pool.execute("UPDATE events SET hidden_by_admin = FALSE WHERE source_url = 'http://aa/1'")
    it = item("http://aa/1", aa, auto_hide_reason(aa))
    it.description = "новое описание"
    await save_events(pool, [it])
    assert await pool.fetchval("SELECT hidden_by_admin FROM events WHERE source_url = 'http://aa/1'") is False
    assert await pool.fetchval("SELECT description FROM events WHERE source_url = 'http://aa/1'") == "новое описание"

    # новая дата (новая ссылка) — скрывается сама
    await save_events(pool, [item("http://aa/2", aa, auto_hide_reason(aa))])
    assert await pool.fetchval("SELECT hidden_by_admin FROM events WHERE source_url = 'http://aa/2'") is True


@pg
async def test_hidden_by_dedup_or_admin_not_unhidden_by_resave(pool):
    await save_events(pool, [item("http://x/1", "Концерт")])
    await pool.execute("UPDATE events SET hidden_by_admin = TRUE WHERE source_url = 'http://x/1'")
    it = item("http://x/1", "Концерт")
    it.description = "обновили"
    await save_events(pool, [it])
    assert await pool.fetchval("SELECT hidden_by_admin FROM events WHERE source_url = 'http://x/1'") is True
