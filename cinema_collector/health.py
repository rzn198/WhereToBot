"""Контроль здоровья скраперов кинотеатров.

Раньше сбой скрапера (сайт переехал, API сменился) только писался в лог, а афиша молча
устаревала: CineGrand и MTS так проработали неделями. Теперь после каждого прогона:

  * каждый источник получает статус ok/BROKEN и строку CINEMA_HEALTH в лог;
  * при переходе в BROKEN админу уходит сообщение, раз в `remind_hours` — напоминание,
    при восстановлении — 🟢;
  * признаки поломки: скрапер упал; 0 сеансов; сеансы не сохранились; все сеансы в прошлом;
    сеансов стало меньше `drop_ratio` от обычного.

«Обычное» число берётся из БД (сколько строк источника было до замены) и из медианы
последних прогонов в памяти — берём бо́льшее, поэтому после рестарта контейнера защита
не обнуляется. Данные при этом НЕ блокируются: монитор только сообщает.
"""
from __future__ import annotations

import statistics
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from loguru import logger

from shared.config import config

HISTORY_SIZE = 7
MIN_HISTORY = 2
DEFAULT_DROP_RATIO = 0.5
DEFAULT_REMIND_HOURS = 24
BELGRADE_TZ = ZoneInfo('Europe/Belgrade')


@dataclass
class RunResult:
    source: str
    events: int = 0                    # сеансов вернул скрапер
    saved: int = 0                     # сеансов записано в БД
    movies: int = 0
    seconds: float = 0.0
    error: str | None = None
    previous: int | None = None        # сколько строк источника было в БД до прогона
    newest: datetime | None = None     # самый поздний сеанс из прогона


def _settings() -> dict:
    cfg = (config.get('cinema_collector') or {}).get('health') or {}
    return {
        'enabled': bool(cfg.get('enabled', True)),
        'drop_ratio': float(cfg.get('drop_ratio', DEFAULT_DROP_RATIO)),
        'remind_hours': float(cfg.get('remind_hours', DEFAULT_REMIND_HOURS)),
        'alerts_in_local': bool(cfg.get('alerts_in_local', False)),
    }


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=BELGRADE_TZ)


class HealthMonitor:

    def __init__(self):
        self._history: dict[str, deque] = {}
        self._status: dict[str, str] = {}        # source -> 'ok' | 'broken'
        self._last_alert: dict[str, float] = {}

    def baseline(self, r: RunResult) -> float:
        hist = self._history.get(r.source)
        from_hist = statistics.median(hist) if hist and len(hist) >= MIN_HISTORY else 0
        return max(r.previous or 0, from_hist)

    def evaluate(self, r: RunResult, drop_ratio: float = DEFAULT_DROP_RATIO,
                 now: datetime | None = None) -> tuple[bool, str]:
        if r.error:
            return False, f"скрапер упал: {r.error}"
        if r.events == 0:
            return False, "0 сеансов (сайт/API недоступен или изменилась вёрстка)"
        if r.saved == 0:
            return False, f"сеансы получены ({r.events}), но не сохранены в БД"
        now = now or datetime.now(timezone.utc)
        if r.newest is not None and _aware(r.newest) < now:
            return False, "все полученные сеансы уже в прошлом"
        base = self.baseline(r)
        if base and r.events < base * drop_ratio:
            return False, f"сеансов стало {r.events} против обычных ~{base:.0f}"
        return True, ""

    def process(self, results: list[RunResult], now: float | None = None,
                drop_ratio: float = DEFAULT_DROP_RATIO,
                remind_hours: float = DEFAULT_REMIND_HOURS,
                now_dt: datetime | None = None) -> list[str]:
        now = time.time() if now is None else now
        messages: list[str] = []
        for r in results:
            ok, reason = self.evaluate(r, drop_ratio, now_dt)
            prev = self._status.get(r.source)
            logger.info(
                f"CINEMA_HEALTH | source={r.source} | status={'ok' if ok else 'BROKEN'} | "
                f"events={r.events} | movies={r.movies} | saved={r.saved} | "
                f"was={'?' if r.previous is None else r.previous} | seconds={r.seconds:.1f}"
                + (f" | reason={reason}" if reason else "")
            )
            if ok:
                self._history.setdefault(r.source, deque(maxlen=HISTORY_SIZE)).append(r.events)
                if prev == 'broken':
                    messages.append(f"🟢 {r.source}: восстановился ({r.events} сеансов, {r.movies} фильмов)")
                    self._last_alert.pop(r.source, None)
                self._status[r.source] = 'ok'
                continue

            if prev != 'broken':
                messages.append(f"🔴 {r.source}: {reason}")
                self._last_alert[r.source] = now
            elif now - self._last_alert.get(r.source, 0) >= remind_hours * 3600:
                messages.append(f"🔴 {r.source}: всё ещё не работает — {reason}")
                self._last_alert[r.source] = now
            self._status[r.source] = 'broken'
        return messages


monitor = HealthMonitor()


async def notify_admin(text: str) -> bool:
    tg = config.get('telegram') or {}
    token, admin_id = tg.get('bot_token'), tg.get('admin_user_id')
    if not token or not admin_id:
        logger.warning("CINEMA_HEALTH_ALERT_SKIPPED | нет TELEGRAM_BOT_TOKEN/ADMIN_USER_ID")
        return False
    try:
        from telegram import Bot
        async with Bot(token) as bot:
            await bot.send_message(chat_id=admin_id, text=text)
        return True
    except Exception as e:
        logger.error(f"CINEMA_HEALTH_ALERT_ERROR | {e}")
        return False


async def report(results: list[RunResult]) -> list[str]:
    """Оценить прогон и разослать алерты. Никогда не бросает исключений."""
    try:
        s = _settings()
        if not s['enabled']:
            return []
        messages = monitor.process(results, drop_ratio=s['drop_ratio'], remind_hours=s['remind_hours'])
        if not messages:
            return []
        env = config.get('env', 'production')
        if env == 'local' and not s['alerts_in_local']:
            for m in messages:
                logger.warning(f"CINEMA_HEALTH_ALERT_LOCAL | {m}")
            return messages
        for m in messages:
            await notify_admin(f"[cinema_collector · {env}]\n{m}")
        return messages
    except Exception as e:
        logger.error(f"CINEMA_HEALTH_REPORT_ERROR | {e}")
        return []
