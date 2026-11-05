"""Запись для сравнения: пост канала (messages) или событие сайта (events)."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date, datetime

KIND_MESSAGE = 'm'
KIND_EVENT = 'e'


@dataclass(frozen=True)
class Record:
    kind: str                       # 'm' | 'e'
    id: int                         # messages.surrogate_id | events.id
    day: date
    source: str                     # 'ch:<channel_id>' | 'event:<events.source>'
    text: str                       # пост целиком | название + место + описание
    hidden: bool = False            # hidden_by_admin
    position: int = 0               # messages.position: > 0 — строка дайджеста
    created_at: datetime | None = None
    city: str | None = None

    @property
    def key(self) -> tuple[str, int]:
        return (self.kind, self.id)

    @property
    def is_digest(self) -> bool:
        """Строка дайджеста (кастомный парсер): целый день со списком событий, скрывать её нельзя."""
        return self.kind == KIND_MESSAGE and self.position > 0

    @property
    def is_list(self) -> bool:
        """Цельный пост-перечень событий: не может ни победить, ни быть скрытым (у строки дайджеста
        position > 0 своя логика — is_digest)."""
        return self.kind == KIND_MESSAGE and self.position == 0 and looks_like_list(self.text)


_LIST_HEAD = re.compile(r'дайджест|подборк|афиша (?:мероприятий|недели|на неделю|выходных)|на выходные и на следующую',
                        re.IGNORECASE)
_TIME_OR_DATE = re.compile(r'\b\d{1,2}[:.]\d{2}\b')
LIST_MARKS_THRESHOLD = 8


def looks_like_list(text: str) -> bool:
    """Пост-перечень многих событий (дайджест, афиша недели, подборка), не разрезанный парсером на строки.
    Эвристика-страховка независимо от AI: слово «дайджест/подборка/афиша недели» в начале либо много
    дат и времён в тексте. Ложное срабатывание безопасно: такая запись просто остаётся видимой."""
    t = text or ''
    return bool(_LIST_HEAD.search(t[:200])) or len(_TIME_OR_DATE.findall(t)) >= LIST_MARKS_THRESHOLD


def norm_text(text: str) -> str:
    """Текст без регистра, пунктуации и эмодзи: для поиска точных копий."""
    return ' '.join(re.findall(r'\w+', (text or '').lower()))


def text_digest(text: str, n: int = 300) -> str:
    return hashlib.sha1(' '.join((text or '').split())[:n].encode('utf-8')).hexdigest()[:12]


def fingerprint(records: list[Record], salt: str) -> str:
    """Отпечаток набора записей даты. Меняется, если запись появилась/пропала/сменила текст или
    видимость; salt — метод, версия промпта и иерархия (смена настроек = пересчёт)."""
    parts = sorted(f"{r.kind}{r.id}:{text_digest(r.text)}:{int(r.hidden)}:{r.source}" for r in records)
    return hashlib.sha1((salt + '|' + '|'.join(parts)).encode('utf-8')).hexdigest()
