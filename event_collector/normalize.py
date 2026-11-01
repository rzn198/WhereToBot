from __future__ import annotations

import html
import re
import unicodedata
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

BELGRADE_TZ = ZoneInfo('Europe/Belgrade')

_TAG_RE = re.compile(r'<[^>]*>')
_WS_RE = re.compile(r'\s+')
_NON_ALNUM_RE = re.compile(r'[^a-z0-9а-яё]+')


def clean_html(raw: str | None) -> str | None:
    if not raw:
        return None
    text = _TAG_RE.sub(' ', raw)
    text = html.unescape(text)          # &nbsp; &amp; &lt; &quot; и пр.
    text = _WS_RE.sub(' ', text).strip()
    return text or None


def parse_datetime(raw: str | datetime | None) -> datetime | None:
    if raw is None:
        return None

    if isinstance(raw, datetime):
        dt = raw
    elif isinstance(raw, str):
        s = raw.strip()
        if not s:
            return None
        if s.endswith('Z'):
            s = s[:-1] + '+00:00'
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
    else:
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=BELGRADE_TZ)
    return dt


def _slug(text: str | None) -> str:
    if not text:
        return ''
    text = text.lower().strip()
    decomposed = unicodedata.normalize('NFKD', text)
    text = ''.join(c for c in decomposed if not unicodedata.combining(c))
    text = _NON_ALNUM_RE.sub('-', text).strip('-')
    return text


def build_dedup_key(
    title: str | None,
    starts_at: datetime | None,
    city: str | None = None,
) -> str | None:
    if not title or not starts_at:
        return None
    day = starts_at.astimezone(BELGRADE_TZ).date().isoformat()
    parts = [_slug(title), day]
    if city:
        parts.append(_slug(city))
    return '|'.join(parts)


def draftjs_to_text(raw: str | None) -> str | None:
    if not raw:
        return None
    s = raw.strip()
    if s.startswith("{"):
        try:
            import json as _json
            data = _json.loads(s)
            blocks = data.get("blocks") or []
            lines = [b.get("text", "") for b in blocks if b.get("text")]
            text = "\n".join(lines).strip()
            return text or None
        except Exception:
            pass
    # не Draft.js — обычная очистка
    return clean_html(raw)
