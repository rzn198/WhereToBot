"""Настройки ИИ-строк списка (config.yaml → card_lines) и опции показа для бота."""
from __future__ import annotations

from dataclasses import dataclass, field

from card_lines.profiles import MODES, Profile, build_profiles


def _int(value, default: int, low: int, high: int) -> int:
    try:
        return min(max(int(value), low), high)
    except (TypeError, ValueError):
        return default


def _float(value, default: float, low: float) -> float:
    try:
        return max(float(value), low)
    except (TypeError, ValueError):
        return default


def _bool(value, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return {'true': True, 'yes': True, 'on': True, '1': True, 'false': False, 'no': False, 'off': False,
                '0': False}.get(value.strip().lower(), default)
    return default if value is None else bool(value)


def _mode(value) -> str:
    if value is True:
        return 'on'                     # YAML 1.1: голое on → True
    if value is False:
        return 'off'
    m = str(value).strip().lower() if value not in (None, '') else 'off'
    return m if m in MODES else 'off'


@dataclass(frozen=True)
class CardLineSettings:
    method: str = 'off'                 # off | shadow (только лог «сколько было бы») | on
    title_max: int = 40                 # длина названия, которую просим у модели (в списке дополнительно обрезается ui.window.title_max)
    input_chars: int = 1200             # сколько символов описания/текста отправляем модели
    batch_size: int = 1                 # записей одного профиля в одном запросе
    concurrency: int = 3
    max_calls_per_run: int = 1500       # запросов за проход; остальное — в следующий проход
    retry_minutes: float = 30
    max_retries: int = 3
    continue_seconds: float = 60        # пауза до следующего прохода, если упёрлись в max_calls_per_run
    run_on_start: bool = True
    after_dedup: bool = True            # брать только записи, уже обработанные дедупликатором (дубли не тратят запросы)
    alerts: bool = True
    emoji: bool = True                  # эмодзи в начале строки (по значению атрибута)
    show_value: bool = True             # слово значения (жанр) после названия
    profiles: dict[str, Profile] = field(default_factory=dict)

    @property
    def enabled(self) -> bool:
        return self.method in ('shadow', 'on')

    @property
    def use_in_bot(self) -> bool:
        return self.method == 'on'


def card_line_settings(cfg: dict | None) -> CardLineSettings:
    c = cfg or {}
    d = CardLineSettings()
    return CardLineSettings(
        method=_mode(c.get('method')),
        title_max=_int(c.get('title_max'), d.title_max, 10, 80),
        input_chars=_int(c.get('input_chars'), d.input_chars, 200, 6000),
        batch_size=_int(c.get('batch_size'), d.batch_size, 1, 20),
        concurrency=_int(c.get('concurrency'), d.concurrency, 1, 20),
        max_calls_per_run=_int(c.get('max_calls_per_run'), d.max_calls_per_run, 1, 100000),
        retry_minutes=_float(c.get('retry_minutes'), d.retry_minutes, 1.0),
        max_retries=_int(c.get('max_retries'), d.max_retries, 0, 20),
        continue_seconds=_float(c.get('continue_seconds'), d.continue_seconds, 5.0),
        run_on_start=_bool(c.get('run_on_start'), d.run_on_start),
        after_dedup=_bool(c.get('after_dedup'), d.after_dedup),
        alerts=_bool(c.get('alerts'), d.alerts),
        emoji=_bool(c.get('emoji'), d.emoji),
        show_value=_bool(c.get('show_value'), d.show_value),
        profiles=build_profiles(c.get('profiles')),
    )


def display_settings() -> CardLineSettings:
    """Настройки для бота: из config.yaml → card_lines (сбой чтения = выключено, строки из данных БД)."""
    try:
        from shared.config import config
        return card_line_settings(config.get('card_lines') or {})
    except Exception:
        return CardLineSettings()
