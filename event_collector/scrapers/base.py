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
    # Откуда категории: site — по категориям самого сайта (надёжно); fixed — константа источника;
    # dict — словарь по тексту; aggregate — грубая категория широкого раздела сайта; default — подстановка скрейпера, когда сайт категорию не дал; none — пусто.
    category_basis: str = 'site'
    # Исходные категории сайта текстом («THEATRE / MONODRAMA») — подсказка для AI-категоризации
    category_hint: str | None = None


class BaseScraper:


    source: str = ''

    async def fetch(self) -> list[EventItem]:

        raise NotImplementedError
