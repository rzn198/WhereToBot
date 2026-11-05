"""Когда запускать дедупликацию: чистое решение по снимку состояния (без БД, удобно тестировать) и загрузка снимка.

Правила:
  * пока работает хотя бы один коллектор (collector, event_collector) — ждём; прогон без пульса дольше
    stale_heartbeat_minutes считается зависшим и закрывается как error;
  * после окончания коллекторов ждём settle_seconds (вдруг стартует следующий) и запускаемся, если с
    начала прошлого прогона дедупликатора коллекторы успели закончить хотя бы один прогон;
  * первый запуск сервиса без единого прогона — сразу (разбираем накопленное);
  * после неудачи (AI недоступен, ошибка) повтор через retry_minutes, не больше max_retries раз подряд;
    дальше — только когда коллекторы принесут новые данные.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import asyncpg
from loguru import logger

from db.tables import Tables

BAD = ('skipped', 'error')


@dataclass(frozen=True)
class ServiceSettings:
    poll_seconds: float = 60
    settle_seconds: float = 180
    stale_heartbeat_minutes: float = 15
    retry_minutes: float = 30
    max_retries: int = 3
    alerts: bool = True


def service_settings(cfg: dict) -> ServiceSettings:
    s = (cfg or {}).get('service') or {}
    d = ServiceSettings()
    return ServiceSettings(
        poll_seconds=max(1.0, float(s.get('poll_seconds', d.poll_seconds))),
        settle_seconds=max(0.0, float(s.get('settle_seconds', d.settle_seconds))),
        stale_heartbeat_minutes=max(1.0, float(s.get('stale_heartbeat_minutes', d.stale_heartbeat_minutes))),
        retry_minutes=max(1.0, float(s.get('retry_minutes', d.retry_minutes))),
        max_retries=max(0, int(s.get('max_retries', d.max_retries))),
        alerts=bool(s.get('alerts', d.alerts)),
    )


@dataclass(frozen=True)
class Snapshot:
    now: datetime
    active_collectors: int = 0                    # работают прямо сейчас (с живым пульсом)
    last_collector_finish: datetime | None = None
    last_run_started: datetime | None = None      # прошлый завершённый прогон дедупликатора
    last_run_status: str | None = None
    bad_streak: int = 0                           # подряд неудачных прогонов (skipped/error) в конце истории


def decide(snap: Snapshot, st: ServiceSettings) -> tuple[bool, str]:
    if snap.active_collectors > 0:
        return False, f"работают коллекторы ({snap.active_collectors})"
    lcf = snap.last_collector_finish
    if lcf is not None and (snap.now - lcf).total_seconds() < st.settle_seconds:
        return False, "коллекторы только закончили, ждём"
    if snap.last_run_started is None:
        return True, "первый прогон"
    if lcf is not None and lcf > snap.last_run_started:
        return True, "коллекторы принесли новые данные"
    if (snap.last_run_status in BAD and snap.bad_streak <= st.max_retries
            and (snap.now - snap.last_run_started) >= timedelta(minutes=st.retry_minutes)):
        return True, f"повтор после неудачи ({snap.last_run_status}, подряд {snap.bad_streak})"
    return False, "новых данных нет"


async def reap_stale(pool: asyncpg.Pool, stale_minutes: float) -> int:
    """Прогоны коллекторов без пульса (контейнер упал) закрываем как error, иначе они блокировали бы дедупликацию."""
    res = await pool.execute(
        f"UPDATE {Tables.COLLECTOR_RUNS} SET status = 'error', finished_at = heartbeat_at "
        f"WHERE finished_at IS NULL AND heartbeat_at < now() - make_interval(mins => $1)", stale_minutes)
    n = int(res.split()[-1])
    if n:
        logger.warning(f"DEDUP_STALE_COLLECTOR_RUNS_CLOSED | rows={n}")
    return n


async def close_interrupted_runs(pool: asyncpg.Pool) -> int:
    """При старте сервиса: прогон дедупликатора, оставшийся 'running' после падения, помечаем как error."""
    res = await pool.execute(
        f"UPDATE {Tables.DEDUP_RUNS} SET status = 'error', finished_at = now(), error = 'прерван перезапуском сервиса' "
        f"WHERE status = 'running'")
    return int(res.split()[-1])


async def load_snapshot(pool: asyncpg.Pool, st: ServiceSettings, now: datetime | None = None) -> Snapshot:
    now = now or datetime.now(timezone.utc)
    await reap_stale(pool, st.stale_heartbeat_minutes)
    active = await pool.fetchval(f"SELECT count(*) FROM {Tables.COLLECTOR_RUNS} WHERE finished_at IS NULL")
    lcf = await pool.fetchval(f"SELECT max(finished_at) FROM {Tables.COLLECTOR_RUNS} WHERE finished_at IS NOT NULL")
    runs = await pool.fetch(
        f"SELECT started_at, status FROM {Tables.DEDUP_RUNS} WHERE status <> 'running' ORDER BY id DESC LIMIT 20")
    streak = 0
    for r in runs:
        if r['status'] not in BAD:
            break
        streak += 1
    return Snapshot(now=now, active_collectors=int(active or 0), last_collector_finish=lcf,
                    last_run_started=runs[0]['started_at'] if runs else None,
                    last_run_status=runs[0]['status'] if runs else None, bad_streak=streak)
