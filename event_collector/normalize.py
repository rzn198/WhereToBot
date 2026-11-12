from __future__ import annotations

import html
import re
import unicodedata
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from loguru import logger

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


# ── Правила скрейперов по названию (автоскрытие, метка «Музеи») ───────────────────────────────────────────────────
def norm_words(text: str | None) -> tuple[str, ...]:
    """Слова текста в нормальной форме: регистр, ё→е, диакритика латиницы снята (č→c, đ→d), знаки препинания — разделители.
    Кириллицу не раскладываем (иначе «й» превратилась бы в «и» и совпадения расширились бы)."""
    out: list[str] = []
    for ch in (text or "").casefold():
        if ch == "ё":
            out.append("е")
        elif ch == "đ":
            out.append("d")
        elif ord(ch) < 0x250:
            out.append("".join(c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c)))
        else:
            out.append(ch)
    return tuple(re.findall(r"[^\W_]+", "".join(out)))


def match_phrase(title: str | None, phrases) -> str | None:
    """Фраза из списка, целиком (подряд, слово в слово) входящая в название; иначе None."""
    words = norm_words(title)
    for phrase in phrases:
        pw = norm_words(phrase)
        n = len(pw)
        if n and any(words[i:i + n] == pw for i in range(len(words) - n + 1)):
            return phrase
    return None


def match_whole(title: str | None, names) -> str | None:
    """Название из списка, равное названию записи целиком (никаких добавок до/после); иначе None."""
    words = norm_words(title)
    if not words:
        return None
    for name in names:
        if norm_words(name) == words:
            return name
    return None


def report_rule_hits(source: str, kind: str, names, hits: list[str | None]) -> None:
    """Итог правила за прогон: сколько записей совпало и по каким названиям. Название из списка без единого совпадения
    в листинге источника — предупреждение (музей переименовали/убрали, список устарел)."""
    counts = {n: 0 for n in names}
    for h in hits:
        if h in counts:
            counts[h] += 1
    logger.info(f"AUTORULE | source={source} | kind={kind} | matched={sum(counts.values())}")
    for n, c in counts.items():
        if c == 0:
            logger.warning(f"AUTORULE_NO_MATCH | source={source} | kind={kind} | rule={n!r} — в листинге нет, список устарел?")
