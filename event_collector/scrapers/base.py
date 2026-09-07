from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class EventItem:

    source:        str
    source_url:    str

    title:         str
    starts_at:     datetime
    description:   str | None = None
    venue:         str | None = None
    city:          str | None = None
    country:       str | None = 'RS'
    event_types:   list[str] = field(default_factory=list)
    images:        str | None = None
    dedup_key:     str | None = None


class BaseScraper:


    source: str = ''

    async def fetch(self) -> list[EventItem]:

        raise NotImplementedError
