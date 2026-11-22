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


def _onoff(value) -> bool:
    """YAML 1.1: голое on/off становится True/False; строки on/off/true/false тоже понимаем."""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ('on', 'true', 'yes', '1') if value not in (None, '') else False


@dataclass(frozen=True)
class EnrichSettings:
    """Обогащение фактами из интернета для записей без достаточного описания (card_lines.hooks.enrich)."""
    enabled: bool = False
    sources: tuple[str, ...] = ('musicbrainz', 'wikipedia')
    languages: tuple[str, ...] = ('ru', 'en', 'sr')
    contact: str = ''                   # email или адрес бота для User-Agent (MusicBrainz и Wikipedia требуют контакт)
    timeout: float = 10.0
    ttl_found_days: int = 90
    ttl_missing_days: int = 14          # «не найдено»: повтор поиска (и хука) через столько дней
    ttl_ambiguous_days: int = 30
    retry_hours: float = 6.0            # сбой сети: повтор хука через столько часов
    mb_min_interval: float = 1.1        # MusicBrainz: не чаще 1 запроса в секунду


@dataclass(frozen=True)
class HookSettings:
    """Хуки строк (card_lines.hooks): фраза после названия по иерархии формул категории."""
    enabled: bool = False
    hook_max: int = 80                  # максимум символов хука
    min_description_chars: int = 40     # короче (после удаления «ORGANIZATOR/IZVOR: ...») = данных нет, сразу обогащение
    enrich: EnrichSettings = field(default_factory=EnrichSettings)

    @property
    def key(self) -> str:
        """Часть хэша входных данных: смена настроек хуков пересчитывает записи."""
        return f"h{self.hook_max}:{int(self.enrich.enabled)}" if self.enabled else ''


def hook_settings(cfg: dict | None) -> HookSettings:
    c = cfg if isinstance(cfg, dict) else {}
    e = c.get('enrich') if isinstance(c.get('enrich'), dict) else {}
    de = EnrichSettings()
    srcs = tuple(str(x).strip().lower() for x in (e.get('sources') or de.sources) if str(x).strip()) or de.sources
    langs = tuple(str(x).strip().lower() for x in (e.get('languages') or de.languages) if str(x).strip()) or de.languages
    enrich = EnrichSettings(
        enabled=_onoff(e.get('method', e.get('enabled'))),
        sources=srcs, languages=langs, contact=str(e.get('contact') or '').strip(),
        timeout=_float(e.get('timeout'), de.timeout, 1.0),
        ttl_found_days=_int(e.get('ttl_found_days'), de.ttl_found_days, 1, 3650),
        ttl_missing_days=_int(e.get('ttl_missing_days'), de.ttl_missing_days, 1, 3650),
        ttl_ambiguous_days=_int(e.get('ttl_ambiguous_days'), de.ttl_ambiguous_days, 1, 3650),
        retry_hours=_float(e.get('retry_hours'), de.retry_hours, 0.1),
        mb_min_interval=_float(e.get('mb_min_interval'), de.mb_min_interval, 1.0),
    )
    d = HookSettings()
    return HookSettings(
        enabled=_onoff(c.get('method', c.get('enabled'))),
        hook_max=_int(c.get('hook_max'), d.hook_max, 20, 100),
        min_description_chars=_int(c.get('min_description_chars'), d.min_description_chars, 0, 2000),
        enrich=enrich,
    )


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
    auto: bool = True                   # false: сервис сам ничего не считает (только вручную: scripts/card_lines_run.py); бот строки показывает
    after_dedup: bool = True            # брать только записи, уже обработанные дедупликатором (дубли не тратят запросы)
    alerts: bool = True
    emoji: bool = True                  # эмодзи в начале строки (по значению атрибута)
    show_value: bool = True             # слово значения (жанр) после названия
    horizon_days: int | None = None     # сколько дней вперёд считать строки (0 = только сегодня, 1 = сегодня и завтра); None — как показывает бот
    only_profiles: tuple[str, ...] = () # считать строки только для этих категорий (пусто — для всех)
    profiles: dict[str, Profile] = field(default_factory=dict)
    hooks: HookSettings = field(default_factory=HookSettings)

    @property
    def enabled(self) -> bool:
        return self.method in ('shadow', 'on')

    @property
    def use_in_bot(self) -> bool:
        return self.method == 'on'

    @property
    def use_hooks(self) -> bool:
        """Бот показывает хуки: card_lines.method on и card_lines.hooks.method on."""
        return self.method == 'on' and self.hooks.enabled


def _horizon(v) -> int | None:
    try:
        return None if v is None or v == '' else max(0, min(int(v), 1000))
    except (TypeError, ValueError):
        return None


def _names(v) -> tuple[str, ...]:
    from shared.categories import normalize_category
    items = v if isinstance(v, (list, tuple)) else [v] if isinstance(v, str) else []
    return tuple(dict.fromkeys(normalize_category(str(x)) for x in items if str(x).strip()))


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
        auto=_bool(c.get('auto'), d.auto),
        after_dedup=_bool(c.get('after_dedup'), d.after_dedup),
        alerts=_bool(c.get('alerts'), d.alerts),
        emoji=_bool(c.get('emoji'), d.emoji),
        show_value=_bool(c.get('show_value'), d.show_value),
        horizon_days=_horizon(c.get('horizon_days')),
        only_profiles=_names(c.get('only_profiles')),
        profiles=build_profiles(c.get('profiles')),
        hooks=hook_settings(c.get('hooks')),
    )


def display_settings() -> CardLineSettings:
    """Настройки для бота: из config.yaml → card_lines (сбой чтения = выключено, строки из данных БД)."""
    try:
        from shared.config import config
        return card_line_settings(config.get('card_lines') or {})
    except Exception:
        return CardLineSettings()
