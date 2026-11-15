import asyncio
import httpx
import pytest
from event_collector.scrapers.biletrs import BiletRsScraper

LISTING = '<script type="application/ld+json">{"itemListElement":[{"item":{"url":"https://app.bilet.rs/events/7/x","startDate":"2026-12-01T20:00:00"}}]}</script>'


def test_listing_tags_collected_for_all_tags(monkeypatch):
    sc = BiletRsScraper()
    async def nosleep(_): pass
    monkeypatch.setattr("event_collector.scrapers.biletrs.asyncio.sleep", nosleep)

    def handler(req):
        tag = req.url.params.get("tag")
        return httpx.Response(200, text=LISTING if tag in ("Koncerti", "Festivali") else "")
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await sc._collect_ids(c)
    assert asyncio.run(run()) == ["7"]
    assert sc._listing_cats["7"] == ["Концерты", "Фестивали"]
