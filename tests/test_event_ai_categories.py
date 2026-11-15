"""AI-категоризация событий сайтов: разбор ответа, режимы, кэш, отказоустойчивость."""
import hashlib
import json
import os
from datetime import datetime

import pytest

from collector.ai.extractor import BadAIOutput
from event_collector.ai_categories import (EventCategoryAI, build_user_prompt, input_hash, parse_response)
from event_collector.scrapers.base import EventItem
from shared.ai import AIUnavailable

ALLOWED = ["Концерты", "Кино/Театр", "Фестивали", "Игры и развлечения", "Выставки/Ярмарки"]


def item(i=1, types=("Концерты",), basis="default", title="Show", hint=None, desc="text"):
    return EventItem(source="efinityrs", source_url=f"https://x/{i}", title=title,
                     starts_at=datetime(2026, 12, 1, 20), description=desc, venue="Zappa",
                     event_types=list(types), category_basis=basis, category_hint=hint)


class FakeAI:
    """Подменяет AIClient: отвечает по очереди из списка (строка — ответ, Exception — ошибка)."""
    def __init__(self, answers, available=True):
        self.answers, self.calls, self._available = list(answers), 0, available
        self.current_model = "fake"

        class S:
            def line(self): return "calls=fake"
        self.stats = S()

    def begin_run(self): pass
    @property
    def available(self): return self._available
    async def aclose(self): pass
    def report_ok(self): pass
    def report_bad_output(self): pass

    async def complete(self, system, user):
        self.calls += 1
        a = self.answers.pop(0) if self.answers else '{"categories": ["Концерты"]}'
        if isinstance(a, Exception):
            raise a
        return a


def svc(client, method="ai", scope="unreliable", **kw):
    cfg = {"method": method, "scope": scope, "max_chars": 1200, "concurrency": 2, "max_calls_per_run": 600, **kw}
    return EventCategoryAI(cfg=cfg, client=client)


@pytest.fixture(autouse=True)
def _allowed(monkeypatch):
    monkeypatch.setattr("event_collector.ai_categories.category_names", lambda: ALLOWED)


def test_parse_response_ok_and_filters_unknown():
    assert parse_response('{"categories": ["Фестивали", "Выдумка", "Концерты", "Фестивали"]}', ALLOWED) == ["Фестивали", "Концерты"]


def test_parse_response_limits_to_three():
    r = parse_response('{"categories": ["Концерты","Кино/Театр","Фестивали","Игры и развлечения"]}', ALLOWED)
    assert len(r) == 3


@pytest.mark.parametrize("raw", ['{"categories": []}', '{"categories": ["Выдумка"]}', '{"x": 1}', '{"categories": "Концерты"}'])
def test_parse_response_rejects(raw):
    with pytest.raises(BadAIOutput):
        parse_response(raw, ALLOWED)


def test_prompt_contains_event_and_whitelist_and_truncates():
    p = build_user_prompt(item(desc="я" * 5000, hint="THEATRE"), ALLOWED, 100)
    assert "Zappa" in p and "THEATRE" in p and "- Фестивали" in p and "я" * 100 + "…" in p and "я" * 101 not in p


def test_hash_changes_with_text_and_not_with_url():
    a, b = item(1), item(2)
    assert input_hash(a, 1200) == input_hash(b, 1200)
    assert input_hash(a, 1200) != input_hash(item(1, desc="другое"), 1200)


async def test_source_mode_never_calls_ai():
    ai = FakeAI([])
    items = [item()]
    c = await svc(ai, method="source").apply(None, items)
    assert ai.calls == 0 and items[0].event_types == ["Концерты"] and c.candidates == 0


async def test_shadow_logs_but_does_not_change():
    ai = FakeAI(['{"categories": ["Кино/Театр"]}'])
    items = [item()]
    c = await svc(ai, method="shadow").apply(None, items)
    assert items[0].event_types == ["Концерты"] and c.disagree == 1 and c.changed == 0


async def test_ai_mode_replaces_categories():
    ai = FakeAI(['{"categories": ["Кино/Театр"]}'])
    items = [item()]
    c = await svc(ai).apply(None, items)
    assert items[0].event_types == ["Кино/Театр"] and c.changed == 1


async def test_scope_unreliable_skips_site_and_fixed():
    ai = FakeAI([])
    items = [item(1, basis="site"), item(2, basis="fixed")]
    await svc(ai).apply(None, items)
    assert ai.calls == 0
    ai2 = FakeAI([])
    await svc(ai2, scope="all").apply(None, [item(1, basis="site")])
    assert ai2.calls == 1


async def test_scope_covers_dict_none_default():
    ai = FakeAI([])
    items = [item(1, basis="dict"), item(2, types=(), basis="none"), item(3, basis="default")]
    await svc(ai).apply(None, items)
    assert ai.calls == 3


async def test_unavailable_ai_keeps_scraper_categories():
    ai = FakeAI([AIUnavailable("down")])
    items = [item(1), item(2)]
    c = await svc(ai).apply(None, items)
    assert [i.event_types for i in items] == [["Концерты"], ["Концерты"]] and c.changed == 0


async def test_not_configured_client_keeps_categories():
    ai = FakeAI([], available=False)
    items = [item()]
    await svc(ai).apply(None, items)
    assert ai.calls == 0 and items[0].event_types == ["Концерты"]


async def test_bad_output_keeps_categories_and_continues():
    ai = FakeAI(["не json", '{"categories": ["Кино/Театр"]}'])
    items = [item(1), item(2)]
    c = await EventCategoryAI(cfg={"method": "ai", "scope": "unreliable", "concurrency": 1}, client=ai).apply(None, items)
    assert items[0].event_types == ["Концерты"] and items[1].event_types == ["Кино/Театр"] and c.failed == 1


async def test_call_cap():
    ai = FakeAI([])
    items = [item(i) for i in range(5)]
    c = await svc(ai, max_calls_per_run=2, concurrency=1).apply(None, items)
    assert ai.calls == 2 and c.asked == 2


async def test_exception_in_ai_never_propagates():
    class Boom(FakeAI):
        async def complete(self, s, u):
            raise RuntimeError("boom")
    items = [item()]
    await svc(Boom([])).apply(None, items)
    assert items[0].event_types == ["Концерты"]


DSN = os.getenv("DEDUP_TEST_DSN")


@pytest.mark.skipif(not DSN, reason="нужна DEDUP_TEST_DSN")
async def test_cache_prevents_second_call_and_invalidates_on_text_change():
    import asyncpg
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
    try:
        await pool.execute("TRUNCATE event_ai_categories")
        ai = FakeAI(['{"categories": ["Кино/Театр"]}'])
        a = [item()]
        await svc(ai).apply(pool, a)
        assert ai.calls == 1 and a[0].event_types == ["Кино/Театр"]
        b = [item()]
        await svc(FakeAI([])).apply(pool, b)           # из кэша, AI не нужен
        assert b[0].event_types == ["Кино/Театр"]
        ai3 = FakeAI(['{"categories": ["Фестивали"]}'])
        c = [item(desc="описание изменилось")]
        await svc(ai3).apply(pool, c)
        assert ai3.calls == 1 and c[0].event_types == ["Фестивали"]
    finally:
        await pool.close()


async def test_scope_all_overrides_and_diffs_are_collected():
    ai = FakeAI(['{"categories": ["Кино/Театр"]}'])
    items = [item(1, basis="site")]
    c = await svc(ai, method="shadow", scope="all").apply(None, items)
    # basis site: категория сайта сохраняется, AI только дополняет
    assert c.diffs == [("Show", "https://x/1", ["Концерты"], ["Концерты", "Кино/Театр"])] and items[0].event_types == ["Концерты"]


async def test_reliable_category_is_kept_and_extended_in_ai_mode():
    ai = FakeAI(['{"categories": ["Кино/Театр"]}'])
    items = [item(types=("Семейные/детские",), basis="site")]
    await svc(ai, scope="all").apply(None, items)
    assert items[0].event_types == ["Семейные/детские", "Кино/Театр"]


async def test_unreliable_category_is_replaced():
    ai = FakeAI(['{"categories": ["Кино/Театр"]}'])
    items = [item(types=("Концерты",), basis="aggregate")]
    await svc(ai).apply(None, items)
    assert items[0].event_types == ["Кино/Театр"]


def test_prompt_limits_festival_and_second_category():
    p = build_user_prompt(item(), ALLOWED, 1200)
    assert "ОДНОЙ" in p and "НЕ являются" in p


def test_prompt_v3_concert_rule():
    """v3: слово «концерт/koncert/concert» в названии — «Концерты» обязательна (случай «Гала-концерт»); версия в хэше кэша."""
    from event_collector import ai_categories as m
    assert m.PROMPT_VERSION == 'v3'
    it = EventItem(source='polako', source_url='u', title='Гала-концерт классической музыки',
                   starts_at=datetime(2026, 11, 20), description='')
    prompt = m.build_user_prompt(it, ['Концерты', 'Кино/Театр'], 1200)
    assert 'концерт' in prompt and 'koncert' in prompt and 'обязательно' in prompt
    assert m.input_hash(it, 1200) != hashlib.md5(
        json.dumps(['v2', it.title, it.venue, it.category_hint, ''], ensure_ascii=False).encode('utf-8')).hexdigest()


@pytest.mark.skipif(not DSN, reason="нужна DEDUP_TEST_DSN")
async def test_hidden_events_are_not_recategorized():
    """Только не скрытые: запись, скрытая в БД, и запись с автоскрытием AI не получают, категории остаются от скрейпера."""
    import asyncpg
    from datetime import date, timedelta
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
    try:
        await pool.execute("TRUNCATE events RESTART IDENTITY CASCADE")
        await pool.execute("TRUNCATE event_ai_categories")
        day = date.today() + timedelta(days=3)
        for url, hid in (("u/visible", False), ("u/hidden", True)):
            await pool.execute(
                "INSERT INTO events (source, source_url, title, venue, starts_at, city, event_types, hidden_by_admin) "
                "VALUES ('polako',$1,'t','v',$2,'Belgrade',ARRAY['Концерты'],$3)", url, day, hid)
        vis, hid_, aa = item(1, basis="dict"), item(2, basis="dict"), item(3, basis="dict")
        vis.source_url, hid_.source_url, aa.source_url = "u/visible", "u/hidden", "u/aa"
        for it in (vis, hid_, aa):
            it.source = "polako"
        aa.auto_hide_reason = "анонимные алкоголики"
        ai = FakeAI(['{"categories": ["Кино/Театр"]}'])
        c = await svc(ai).apply(pool, [vis, hid_, aa])
        assert ai.calls == 1 and c.candidates == 1 and c.skipped_hidden == 1
        assert vis.event_types == ["Кино/Театр"]
        assert hid_.event_types == ["Концерты"] and aa.event_types == ["Концерты"]
        assert await pool.fetchval("SELECT count(*) FROM event_ai_categories") == 1
    finally:
        await pool.close()
