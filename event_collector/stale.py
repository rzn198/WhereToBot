"""Сверка будущих записей источника с его листингом (этап 1: ТОЛЬКО ЛОГ).

Зачем: источник (polako) архивирует/удаляет события, а наша БД о них не знает —
запись остаётся в ленте как «призрак». Здесь мы только измеряем: какие будущие
записи в БД отсутствуют в свежем полном листинге. В events ничего не пишется.

Режимы (config.yaml: event_collector.stale.mode или env EVENT_STALE_MODE):
  off    — ничего не делать
  shadow — считать и писать EVENT_STALE_SHADOW / stale_audit.jsonl (по умолчанию)
Скрытие (mode=hide) — отдельный шаг, по решению после просмотра логов.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from loguru import logger

DEFAULT_MAX_SHARE = 0.3
DEFAULT_MIN_LISTING = 100
MAX_GROUP_LINES = 15


@dataclass
class StaleDecision:
    ok: bool
    reason: str          # '' если ok, иначе причина пропуска
    stale_ids: list[int]


def decide(db_rows: list[dict], listing_urls: set[str] | None, listing_complete: bool,
           max_share: float = DEFAULT_MAX_SHARE,
           min_listing: int = DEFAULT_MIN_LISTING) -> StaleDecision:
    """db_rows: будущие записи источника [{id, source_url, ...}]. Чистая функция."""
    if not listing_complete or listing_urls is None:
        return StaleDecision(False, 'listing_incomplete', [])
    if len(listing_urls) < min_listing:
        return StaleDecision(False, f'listing_too_small({len(listing_urls)}<{min_listing})', [])
    if db_rows and len(listing_urls) < 0.5 * len(db_rows):
        return StaleDecision(False, f'listing_below_half_of_db({len(listing_urls)}<{len(db_rows)}/2)', [])
    stale = [r['id'] for r in db_rows if r['source_url'] not in listing_urls]
    if db_rows and len(stale) / len(db_rows) > max_share:
        return StaleDecision(False, f'share_too_high({len(stale)}/{len(db_rows)}>{max_share:.0%})', [])
    return StaleDecision(True, '', stale)


def _cfg() -> dict:
    from shared.config import config
    cfg = ((config.get('event_collector', {}) or {}).get('stale', {}) or {})
    mode = os.environ.get('EVENT_STALE_MODE') or cfg.get('mode', 'shadow')
    return {
        'mode': str(mode).lower(),
        'max_share': float(cfg.get('max_share', DEFAULT_MAX_SHARE)),
        'min_listing': int(cfg.get('min_listing', DEFAULT_MIN_LISTING)),
    }


def _audit_path() -> Path:
    from shared.logging import LOGS_DIR
    return Path(LOGS_DIR) / 'stale_audit.jsonl'


async def run_shadow(pool, source: str, scraper, cfg: dict | None = None,
                     audit_path: Path | None = None, today: date | None = None) -> StaleDecision | None:
    """Сравнить БД с листингом и записать лог. Никогда не меняет events."""
    cfg = cfg or _cfg()
    if cfg['mode'] == 'off':
        return None
    today = today or date.today()
    rows = await pool.fetch(
        "SELECT id, source_url, title, starts_at, hidden_by_admin FROM events "
        "WHERE source = $1 AND starts_at >= $2", source, today)
    rows = [dict(r) for r in rows]
    d = decide(rows, getattr(scraper, 'listing_urls', None),
               bool(getattr(scraper, 'listing_complete', False)),
               cfg['max_share'], cfg['min_listing'])
    if not d.ok:
        logger.warning(f"EVENT_STALE_SKIP | source={source} | reason={d.reason} | db_future={len(rows)}")
        return d

    by_id = {r['id']: r for r in rows}
    stale = [by_id[i] for i in d.stale_ids]
    hidden = sum(1 for r in stale if r['hidden_by_admin'])
    share = (len(stale) / len(rows) * 100) if rows else 0.0
    logger.info(
        f"EVENT_STALE_SHADOW | source={source} | db_future={len(rows)} | "
        f"listing={len(scraper.listing_urls)} | stale={len(stale)} | already_hidden={hidden} | share={share:.1f}%"
    )
    by_day: dict = {}
    for r in stale:
        by_day.setdefault(r['starts_at'], []).append(r)
    for day in sorted(by_day)[:MAX_GROUP_LINES]:
        grp = by_day[day]
        logger.info(f"EVENT_STALE_GROUP | source={source} | date={day} | n={len(grp)} | e.g. {grp[0]['title'][:60]!r}")

    try:
        path = audit_path or _audit_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        rec = {
            'ts': datetime.now(timezone.utc).isoformat(), 'source': source,
            'db_future': len(rows), 'listing': len(scraper.listing_urls), 'stale': len(stale),
            'items': [{'id': r['id'], 'date': str(r['starts_at']), 'title': r['title'],
                       'hidden': bool(r['hidden_by_admin'])} for r in stale],
        }
        with path.open('a', encoding='utf-8') as f:
            f.write(json.dumps(rec, ensure_ascii=False) + '\n')
    except Exception as e:
        logger.warning(f"EVENT_STALE_AUDIT_ERROR | {e}")
    return d
