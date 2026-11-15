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
    tg_id: int | None = None        # номер поста в Telegram-канале (messages.id): для ссылки t.me в журнале
    url: str | None = None          # events.source_url

    @property
    def key(self) -> tuple[str, int]:
        return (self.kind, self.id)

    @property
    def is_digest(self) -> bool:
        """Строка дайджеста (кастомный парсер, position > 0): «лёгкая» запись об одном событии. Побеждать не может,
        проиграть может (другой записи, ИИ-группе или как точная копия — двойник при сдвиге позиций)."""
        return self.kind == KIND_MESSAGE and self.position > 0

    @property
    def is_list(self) -> bool:
        """Цельный пост-перечень событий (position = 0): не может ни победить, ни быть скрытым."""
        return self.kind == KIND_MESSAGE and self.position == 0 and looks_like_list(self.text)


# --- определение перечня -------------------------------------------------------------------------------
# Пост-перечень (дайджест, афиша недели) — это ОДНА запись про много событий: скрыть её из-за одного дубля
# нельзя. Признаки (ТЗ Denis, ноябрь 2026, проверены на 20 размеченных постах):
#   * сильное слово в первых STRONG_WINDOW символах — достаточно одного;
#   * ИЛИ слабое слово в первых WEAK_WINDOW символах И не меньше WEAK_MIN_TIMES времён в тексте.
# «Много времён без слов» перечнем больше НЕ считается (так терялся French VR с сеансами по часам).
# Слова и окна можно переопределить в config.yaml: deduplication.ai.list_markers (см. configure_list_markers).
DEFAULT_STRONG = ('дайджест', r'афиша\s+(?:мероприятий|недели|на\s+неделю|выходных)')
DEFAULT_WEAK = ('подборк', r'на\s+выходные\s+и\s+на\s+следующую', r'куда\s+пойти', r'анонсы?\s+(?:на\s+)?недел')
DEFAULT_STRONG_WINDOW = 80
DEFAULT_WEAK_WINDOW = 200
DEFAULT_WEAK_MIN_TIMES = 8

_TIME_OR_DATE = re.compile(r'\b\d{1,2}[:.]\d{2}\b')


@dataclass(frozen=True)
class ListMarkers:
    strong: re.Pattern
    weak: re.Pattern
    strong_window: int = DEFAULT_STRONG_WINDOW
    weak_window: int = DEFAULT_WEAK_WINDOW
    weak_min_times: int = DEFAULT_WEAK_MIN_TIMES

    @property
    def signature(self) -> str:
        """Для отпечатка настроек: смена маркеров = пересчёт дат."""
        return f"{self.strong.pattern}|{self.weak.pattern}|{self.strong_window}|{self.weak_window}|{self.weak_min_times}"


def _compile(words) -> re.Pattern:
    return re.compile('|'.join(f'(?:{w})' for w in words), re.IGNORECASE)


def build_list_markers(cfg: dict | None = None) -> ListMarkers:
    """cfg — блок deduplication.ai.list_markers (может быть пустым: тогда значения по умолчанию).
    Неверное регулярное выражение — ValueError с понятным текстом (сервис не стартует с кривым конфигом)."""
    cfg = cfg or {}
    strong = [str(w) for w in (cfg.get('strong') or DEFAULT_STRONG) if str(w).strip()]
    weak = [str(w) for w in (cfg.get('weak') or DEFAULT_WEAK) if str(w).strip()]
    try:
        return ListMarkers(
            strong=_compile(strong), weak=_compile(weak),
            strong_window=int(cfg.get('strong_window', DEFAULT_STRONG_WINDOW)),
            weak_window=int(cfg.get('weak_window', DEFAULT_WEAK_WINDOW)),
            weak_min_times=int(cfg.get('weak_min_times', DEFAULT_WEAK_MIN_TIMES)))
    except re.error as e:
        raise ValueError(f"deduplication.ai.list_markers: неверное выражение: {e}") from e


_MARKERS = build_list_markers()


def configure_list_markers(cfg: dict | None) -> ListMarkers:
    """Применить маркеры из конфига (вызывает runner при старте прогона)."""
    global _MARKERS
    _MARKERS = build_list_markers(cfg)
    return _MARKERS


def list_markers() -> ListMarkers:
    return _MARKERS


def looks_like_list(text: str, markers: ListMarkers | None = None) -> bool:
    """Пост-перечень многих событий (дайджест, афиша недели, подборка), не разрезанный парсером на строки.
    Ложное срабатывание безопасно: такая запись просто остаётся видимой (но и не скрывается как дубль)."""
    m = markers or _MARKERS
    t = text or ''
    if m.strong.search(t[:m.strong_window]):
        return True
    return bool(m.weak.search(t[:m.weak_window])) and len(_TIME_OR_DATE.findall(t)) >= m.weak_min_times


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
