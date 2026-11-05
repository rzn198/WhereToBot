"""Иерархия источников: кто побеждает при дубле.

Единый список в конфиге, сверху вниз (выше — побеждает):
    deduplication.ai.priority:
      - "@channel_a"        канал (по @username из таблицы channels)
      - "ch:1234567890"     канал по числовому id
      - "event:kupikarte"   источник events (значение events.source)
Не указанные в списке — ниже всех: сначала посты каналов, затем события; при равенстве выигрывает
запись с более длинным текстом, затем более ранняя.
"""
from __future__ import annotations

from loguru import logger

from deduplicator.records import KIND_MESSAGE, Record

UNLISTED = 10 ** 6


def _norm(entry: str) -> str:
    e = str(entry).strip().lower()
    if e.startswith('id:'):
        e = 'ch:' + e[3:]
    if e.startswith(('ch:', 'event:')):
        return e.replace(' ', '')
    return '@' + e.lstrip('@')


class PriorityResolver:
    def __init__(self, entries: list[str] | None = None, channel_names: dict[int, str] | None = None):
        self.entries = [_norm(e) for e in (entries or []) if str(e).strip()]
        self.index = {}
        for i, e in enumerate(self.entries):
            self.index.setdefault(e, i)
        self.channel_names = {int(k): '@' + str(v).lstrip('@').lower()
                              for k, v in (channel_names or {}).items() if v}

    def label(self, rec: Record) -> str:
        """Читаемое имя источника: @username, ch:<id> или event:<source>."""
        if rec.source.startswith('ch:'):
            name = self.channel_names.get(int(rec.source[3:]))
            return name or rec.source
        return rec.source

    def position(self, rec: Record) -> int:
        if rec.source.startswith('ch:'):
            cid = int(rec.source[3:])
            for key in (rec.source, self.channel_names.get(cid)):
                if key and key in self.index:
                    return self.index[key]
            return UNLISTED
        return self.index.get(rec.source.lower(), UNLISTED)

    def rank(self, rec: Record) -> tuple:
        """Меньше — лучше (побеждает)."""
        pos = self.position(rec)
        kind_rank = 0 if rec.kind == KIND_MESSAGE else 1 if pos == UNLISTED else 0
        created = rec.created_at.timestamp() if rec.created_at else 0.0
        return (pos, kind_rank, -len(rec.text or ''), created, rec.id)

    def unresolved(self) -> list[str]:
        """Каналы из списка, которых нет в таблице channels (имя не удастся сопоставить)."""
        known = set(self.channel_names.values())
        return [e for e in self.entries if e.startswith('@') and e not in known]

    def warn_unresolved(self) -> None:
        bad = self.unresolved()
        if bad:
            logger.warning(f"DEDUP_PRIORITY_UNRESOLVED | {bad} — канала нет в таблице channels "
                           "(коллектор записывает её при очередном прогоне); пока эти записи идут как не указанные")
