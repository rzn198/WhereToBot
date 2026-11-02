"""Кэш «тяжёлых» запросов скраперов (описания фильмов) между прогонами.

Скраперы создаются заново на каждый прогон, поэтому их собственные словари не помнят
прошлое: раз в 6 часов заново скачивались одни и те же страницы описаний.
Здесь — общий для процесса кэш с временем жизни (по умолчанию 24 часа).

Правила:
  * кэшируется только удачный результат (`ok(value)` истинно) — сбой не «залипает» на сутки;
  * `cinema_collector.detail_cache_hours: 0` в config.yaml полностью отключает кэш;
  * потокобезопасен: скраперы работают в параллельных потоках.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

DEFAULT_TTL_HOURS = 24
MAX_ITEMS = 5000
MISS = object()


def _ttl_seconds_from_config() -> float:
    try:
        from shared.config import config
        hours = (config.get('cinema_collector') or {}).get('detail_cache_hours', DEFAULT_TTL_HOURS)
        return float(hours) * 3600
    except Exception:
        return DEFAULT_TTL_HOURS * 3600


class TTLCache:

    def __init__(self, ttl_seconds: float | None = None, max_items: int = MAX_ITEMS,
                 clock: Callable[[], float] = time.monotonic):
        self._ttl = ttl_seconds
        self._max = max_items
        self._clock = clock
        self._data: dict[Any, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    @property
    def ttl(self) -> float:
        return self._ttl if self._ttl is not None else _ttl_seconds_from_config()

    def get(self, key):
        ttl = self.ttl
        if ttl <= 0:
            return MISS
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return MISS
            stored_at, value = item
            if self._clock() - stored_at >= ttl:
                del self._data[key]
                return MISS
            return value

    def set(self, key, value) -> None:
        if self.ttl <= 0:
            return
        with self._lock:
            if len(self._data) >= self._max and key not in self._data:
                oldest = min(self._data, key=lambda k: self._data[k][0])
                del self._data[oldest]
            self._data[key] = (self._clock(), value)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)


detail_cache = TTLCache()


def cached_fetch(namespace: str, key: str, fetch: Callable[[], Any],
                 ok: Callable[[Any], bool] = bool) -> Any:
    """Вернуть значение из кэша или получить через fetch(); положить в кэш только удачное."""
    full_key = (namespace, key)
    value = detail_cache.get(full_key)
    if value is not MISS:
        return value
    value = fetch()
    if ok(value):
        detail_cache.set(full_key, value)
    return value
