"""Журнал оценённых AI постов и параллельные запросы коллектора (нужна DEDUP_TEST_DSN)."""
import asyncio
import os
from datetime import date, datetime, timedelta, timezone

import asyncpg
import pytest
from telethon.tl.types import Message, PeerChannel

from collector import channel_reader as cr
from collector.ai.service import AIParsingService
from db import ai_seen

DSN = os.getenv("DEDUP_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="нужна DEDUP_TEST_DSN (Postgres со схемой проекта)")
CH = "@testch"
CID = 777


@pytest.fixture
async def pool():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    for t in ("ai_parse_seen", "messages", "channels"):
        await p.execute(f"TRUNCATE {t} RESTART IDENTITY CASCADE")
    yield p
    await p.close()


class FakeClient:
    def __init__(self, msgs):
        self.msgs = msgs

    async def get_entity(self, ch):
        return type("E", (), {"id": CID, "username": "testch", "title": "T"})()

    async def get_messages(self, entity, limit=100):
        return self.msgs


def msg(i, text, days_ago=0):
    return Message(id=i, peer_id=PeerChannel(CID), message=text,
                   date=datetime.now(timezone.utc) - timedelta(days=days_ago))


class FakeAI:
    """Подменяет ai_service: считает вызовы и одновременность."""
    def __init__(self, answer_date, answered=True, delay=0.0):
        self.answer_date, self.answered, self.delay = answer_date, answered, delay
        self.calls, self.active, self.peak = [], 0, 0
        self.concurrency = 3

    def decides_date(self, channel):
        return True

    def needs_ai(self, text, channel, position=0):
        return True

    async def resolve_full(self, *, text, channel, position, published_at, regex_date, dict_types):
        self.calls.append(text)
        self.active += 1
        self.peak = max(self.peak, self.active)
        await asyncio.sleep(self.delay)
        self.active -= 1
        return (self.answer_date, ["music"], self.answered) if self.answered else (regex_date, dict_types, False)


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setattr(cr, "config", {**cr.config, "cleanup": {"event_cleanup_days": 7, "no_date_cleanup_days": 14},
                                      "photo": {"caption_max_length": 900}})

    def install(ai):
        monkeypatch.setattr(cr, "ai_service", ai)
    return install


OLD = date.today() - timedelta(days=30)
NEW = date.today() + timedelta(days=5)


async def run(pool, msgs):
    return await cr.fetch_new_messages(FakeClient(msgs), CH, pool)


async def test_expired_post_not_sent_to_ai_twice(pool, setup):
    ai = FakeAI(OLD)
    setup(ai)
    msgs = [msg(1, "Концерт давно прошёл, 3 октября")]
    assert await run(pool, msgs) == 0
    assert len(ai.calls) == 1
    assert await pool.fetchval("SELECT count(*) FROM ai_parse_seen") == 1
    await run(pool, msgs)
    await run(pool, msgs)
    assert len(ai.calls) == 1          # повторно в AI не ходили


async def test_edited_text_is_reevaluated(pool, setup):
    ai = FakeAI(OLD)
    setup(ai)
    await run(pool, [msg(1, "Старый текст")])
    ai.answer_date = NEW
    assert await run(pool, [msg(1, "Текст исправили: концерт скоро")]) == 1
    assert len(ai.calls) == 2


async def test_saved_post_not_sent_again(pool, setup):
    ai = FakeAI(NEW)
    setup(ai)
    assert await run(pool, [msg(1, "Будущее событие")]) == 1
    assert await run(pool, [msg(1, "Будущее событие")]) == 0
    assert len(ai.calls) == 1


async def test_ai_unavailable_not_marked_seen(pool, setup):
    ai = FakeAI(None, answered=False)
    setup(ai)
    old = [msg(1, "Пост без ответа AI", days_ago=20)]      # без даты и старше no_date_cleanup_days
    await run(pool, old)
    assert await pool.fetchval("SELECT count(*) FROM ai_parse_seen") == 0
    await run(pool, old)
    assert len(ai.calls) == 2          # AI вернётся — оценим


async def test_requests_run_in_parallel_up_to_limit(pool, setup):
    ai = FakeAI(NEW, delay=0.05)
    setup(ai)
    msgs = [msg(i, f"Событие номер {i}") for i in range(1, 9)]
    assert await run(pool, msgs) == 8
    assert 1 < ai.peak <= 3


async def test_seen_helpers(pool):
    await ai_seen.mark_seen(pool, 1, 2, 0, "abc")
    assert await ai_seen.load_seen(pool, 1, [2, 3]) == {(2, 0): ai_seen.text_hash("abc")}
    await pool.execute("UPDATE ai_parse_seen SET seen_at = now() - interval '90 days'")
    assert await ai_seen.purge_old(pool, 60) == 1
    assert await ai_seen.load_seen(pool, 1, [2]) == {}


async def test_service_resolve_full_flags():
    svc = AIParsingService(cfg={"date_method": "regex", "category_method": "dict"}, client=None)
    d, t, answered = await svc.resolve_full(text="x", channel="@c", position=0, published_at=date.today(),
                                            regex_date=None, dict_types=[])
    assert answered is False


def test_old_post_with_old_date_is_valid_answer():
    """Пост 2024 года с датой 2024: нормальный ответ (не BadAIOutput и не повод менять модель)."""
    from collector.ai import extractor
    raw = '{"date":"2024-11-21","categories":["Концерты"]}'
    r = extractor.parse_response(raw, ["Концерты"], date(2024, 11, 18))
    assert r.date == date(2024, 11, 21)
    with pytest.raises(extractor.BadAIOutput):
        extractor.parse_response('{"date":"2020-01-01","categories":[]}', ["Концерты"], date(2024, 11, 18))
