# event_collector/health.py
# Мониторинг скраперов: тихая поломка источника (как было с ticrs, который
# неделями отдавал 0 событий) должна доходить до админа сообщением в Telegram.
#
# Метрика — размер ЛИСТИНГА источника (`scraper.listed_count`), а не число
# сохранённых событий: при known_urls на обычном прогоне новых событий почти
# нет, и «0 новых» — норма. Источник считается сломанным, если:
#   - скрапер упал с исключением;
#   - листинг не получен (listed_count остался None) или пуст (0);
#   - листинг резко сократился относительно медианы последних здоровых
#     прогонов (по умолчанию меньше 50%).
# Алерт шлётся при переходе «здоров -> сломан», напоминание — раз в
# remind_hours, а при восстановлении — сообщение «восстановился».
# Состояние хранится в памяти процесса event_collector.
#
# Настройки (config.yaml, все необязательны):
#   event_collector:
#     health:
#       enabled: true
#       drop_ratio: 0.5
#       remind_hours: 24
#       alerts_in_local: false   # при APP_ENV=local только лог, без Telegram

from __future__ import annotations

import statistics
import time
from collections import deque
from dataclasses import dataclass

from loguru import logger

from shared.config import config

HISTORY_SIZE = 7
MIN_HISTORY = 2
DEFAULT_DROP_RATIO = 0.5
DEFAULT_REMIND_HOURS = 24


@dataclass
class RunResult:
    source: str
    saved: int = 0
    seconds: float = 0.0
    error: str | None = None
    # False — скрапер не сообщает размер листинга (не оцениваем)
    reports: bool = True
    listed: int | None = None


def _settings() -> dict:
    cfg = (config.get('event_collector') or {}).get('health') or {}
    return {
        'enabled': bool(cfg.get('enabled', True)),
        'drop_ratio': float(cfg.get('drop_ratio', DEFAULT_DROP_RATIO)),
        'remind_hours': float(cfg.get('remind_hours', DEFAULT_REMIND_HOURS)),
        'alerts_in_local': bool(cfg.get('alerts_in_local', False)),
    }


class HealthMonitor:

    def __init__(self):
        self._history: dict[str, deque] = {}
        self._status: dict[str, str] = {}        # source -> 'ok' | 'broken'
        self._last_alert: dict[str, float] = {}

    def evaluate(self, r: RunResult, drop_ratio: float = DEFAULT_DROP_RATIO):
        """(ok, причина). Не меняет состояние."""
        if r.error:
            return False, f"скрапер упал: {r.error}"
        if not r.reports:
            return True, ""
        if r.listed is None:
            return False, "листинг не получен (ошибка запроса или разбора)"
        if r.listed == 0:
            return False, "листинг пуст (0 событий)"
        hist = self._history.get(r.source)
        if hist and len(hist) >= MIN_HISTORY:
            base = statistics.median(hist)
            if r.listed < base * drop_ratio:
                return False, (
                    f"листинг сократился: {r.listed} против обычных ~{base:.0f}"
                )
        return True, ""

    def process(self, results: list[RunResult], now: float | None = None,
                drop_ratio: float = DEFAULT_DROP_RATIO,
                remind_hours: float = DEFAULT_REMIND_HOURS) -> list[str]:
        """Обновляет состояние и возвращает тексты алертов."""
        now = time.time() if now is None else now
        messages: list[str] = []
        for r in results:
            ok, reason = self.evaluate(r, drop_ratio)
            prev = self._status.get(r.source)
            listed = "?" if r.listed is None else r.listed
            logger.info(
                f"EVENT_HEALTH | source={r.source} | "
                f"status={'ok' if ok else 'BROKEN'} | listed={listed} | "
                f"saved={r.saved} | seconds={r.seconds:.1f}"
                + (f" | reason={reason}" if reason else "")
            )
            if ok:
                if r.reports and r.listed:
                    self._history.setdefault(
                        r.source, deque(maxlen=HISTORY_SIZE)).append(r.listed)
                if prev == 'broken':
                    messages.append(
                        f"🟢 {r.source}: восстановился (в листинге {listed})"
                    )
                    self._last_alert.pop(r.source, None)
                self._status[r.source] = 'ok'
                continue

            # сломан: не пишем сломанные значения в историю
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
    """Отправляет сообщение админу от имени основного бота. Не бросает."""
    tg = config.get('telegram') or {}
    token, admin_id = tg.get('bot_token'), tg.get('admin_user_id')
    if not token or not admin_id:
        logger.warning("HEALTH_ALERT_SKIPPED | нет TELEGRAM_BOT_TOKEN/ADMIN_USER_ID")
        return False
    try:
        from telegram import Bot
        async with Bot(token) as bot:
            await bot.send_message(chat_id=admin_id, text=text)
        return True
    except Exception as e:
        logger.error(f"HEALTH_ALERT_ERROR | {e}")
        return False


async def report(results: list[RunResult]) -> list[str]:
    """Оценивает прогон и рассылает алерты. Любая ошибка гасится: мониторинг
    не должен ронять коллектор."""
    try:
        s = _settings()
        if not s['enabled']:
            return []
        messages = monitor.process(
            results, drop_ratio=s['drop_ratio'], remind_hours=s['remind_hours'])
        if not messages:
            return []
        env = config.get('env', 'production')
        if env == 'local' and not s['alerts_in_local']:
            for m in messages:
                logger.warning(f"HEALTH_ALERT_LOCAL | {m}")
            return messages
        for m in messages:
            await notify_admin(f"[event_collector · {env}]\n{m}")
        return messages
    except Exception as e:
        logger.error(f"HEALTH_REPORT_ERROR | {e}")
        return []
