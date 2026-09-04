# event_collector/scrapers/base.py
# Базовый класс и единая структура данных для всех скраперов событий.
# Поля EventItem == колонки таблицы events (1:1, кроме служебных id/created_at/updated_at).

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class EventItem:
    """Единый формат события для всех региональных скраперов."""

    # --- идентичность ---
    source:        str                  # 'efinityrs', 'polako', ...
    source_url:    str                  # UNIQUE-ключ дедупа/UPSERT

    # --- ядро ---
    title:         str
    starts_at:     datetime             # уже нормализованный (tz-aware)

    # --- опциональное ядро ---
    description:   str | None = None

    # --- локация ---
    venue:         str | None = None
    city:          str | None = None
    country:       str | None = 'RS'

    # --- классификация ---
    event_types:   list[str] = field(default_factory=list)   # мульти-лейбл (categorizer_v2)

    # --- вложенное (JSONB) ---
    images:        list[str] = field(default_factory=list)

    # --- дедуп между источниками (заполняется normalize, используется позже) ---
    dedup_key:     str | None = None


class BaseScraper:
    """Базовый класс для всех скраперов событий."""

    source: str = ''

    async def fetch(self) -> list[EventItem]:
        """
        Получает все актуальные события источника.
        Возвращает список нормализованных EventItem.
        """
        raise NotImplementedError
