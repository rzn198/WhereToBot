from __future__ import annotations

from datetime import date, datetime
from typing import Mapping


def _as_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    return value


def is_known(known, url: str, listing_start=None) -> bool:
    if url not in known:
        return False
    if not isinstance(known, Mapping):
        return True
    listed = _as_date(listing_start)
    stored = _as_date(known.get(url))
    if listed is None or stored is None:
        return True
    return listed == stored
