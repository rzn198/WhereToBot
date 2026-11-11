"""Журнал решений дедупликатора: JSONL-файл, по которому можно вручную разобрать, кого и почему скрыли.

Файл logs/dedup_audit.jsonl (том logs), одна строка — одно событие, строка самодостаточна (тексты внутри):
  run_start / run_end   параметры и итог прогона;
  day                   сводка по дате (записей, групп, решений);
  ai_raw                сырые ответы модели по дате;
  group                 группа дублей: все участники с ролями winner / loser / kept;
  decision              одно решение «победитель → проигравший» и его происхождение (via);
  kept                  запись осталась видимой, с причиной (digest, list, weak, same_source, ...);
  change                решение прошлого прогона пропало или сменило победителя;
  bad_output, day_error, apply_error   сбои.

Настройки: deduplication.audit (enabled — главный выключатель). Запись журнала никогда не ломает прогон:
любая ошибка записи глушится (один раз пишется в обычный лог).
"""
from __future__ import annotations

import gzip
import json
import logging
import os
import shutil
import time
from datetime import date, datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

from loguru import logger

from deduplicator.records import KIND_EVENT, Record

DEFAULT_NAME = 'dedup_audit.jsonl'
RAW_LIMIT = 20000


def default_path() -> Path:
    from shared.logging import LOGS_DIR
    return Path(LOGS_DIR) / DEFAULT_NAME


class _GzRotating(RotatingFileHandler):
    """Ротация по размеру со сжатием и удалением архивов старше retention_days."""

    def __init__(self, path: Path, max_bytes: int, retention_days: int):
        super().__init__(path, maxBytes=max_bytes, backupCount=1000, encoding='utf-8')
        self.retention_days = retention_days
        self.namer = lambda name: name + '.gz'
        self.rotator = self._gzip
        self._prune()

    @staticmethod
    def _gzip(source: str, dest: str) -> None:
        with open(source, 'rb') as f_in, gzip.open(dest, 'wb') as f_out:
            shutil.copyfileobj(f_in, f_out)
        os.remove(source)

    def _prune(self) -> None:
        if self.retention_days <= 0:
            return
        limit = time.time() - self.retention_days * 86400
        base = Path(self.baseFilename)
        for p in base.parent.glob(base.name + '.*.gz'):
            try:
                if p.stat().st_mtime < limit:
                    p.unlink()
            except OSError:
                pass

    def doRollover(self) -> None:
        super().doRollover()
        self._prune()


class NullAudit:
    """Журнал выключен: все вызовы — пустые."""
    enabled = False

    def emit(self, event: str, **fields) -> None:
        return None

    def log_run_start(self, *a, **k) -> None: ...
    def log_run_end(self, *a, **k) -> None: ...
    def log_day(self, *a, **k) -> None: ...
    def log_bad_output(self, *a, **k) -> None: ...
    def log_error(self, *a, **k) -> None: ...


class AuditLog(NullAudit):
    enabled = True

    def __init__(self, path: Path | str, *, max_text_chars: int = 4000, rotation_mb: int = 50,
                 retention_days: int = 90, raw_answers: bool = True):
        self.path = Path(path)
        self.max_text = max(100, int(max_text_chars))
        self.raw_answers = bool(raw_answers)
        self._failed = False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handler = _GzRotating(self.path, max(1, int(rotation_mb)) * 1024 * 1024, int(retention_days))
        handler.setFormatter(logging.Formatter('%(message)s'))
        self._log = logging.getLogger(f"dedup.audit.{id(self)}")
        self._log.setLevel(logging.INFO)
        self._log.propagate = False
        self._log.addHandler(handler)
        self._handler = handler

    def close(self) -> None:
        self._log.removeHandler(self._handler)
        self._handler.close()

    # ------------------------------------------------------------------ низкий уровень

    def emit(self, event: str, **fields) -> None:
        try:
            row = {'ts': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'event': event}
            row.update(fields)
            self._log.info(json.dumps(row, ensure_ascii=False, default=_default))
        except Exception as e:                                   # журнал не должен ронять прогон
            if not self._failed:
                self._failed = True
                logger.error(f"DEDUP_AUDIT_WRITE_ERROR | {type(e).__name__}: {e} (дальше ошибки журнала не повторяются)")

    def _rec(self, r: Record, resolver, **extra) -> dict:
        text = r.text or ''
        d = {'kind': r.kind, 'id': r.id, 'src': r.source, 'name': resolver.label(r) if resolver else r.source,
             'pos': r.position, 'created': r.created_at.isoformat(timespec='seconds') if r.created_at else None,
             'link': _link(r, resolver), 'text_len': len(text), 'text': text[:self.max_text],
             'truncated': len(text) > self.max_text}
        if r.city:
            d['city'] = r.city
        d.update(extra)
        return d

    # ------------------------------------------------------------------ события прогона

    def log_run_start(self, *, run_id, method, models, resolver, settings, prompt_version, dates_total, dates_ai) -> None:
        self.emit('run_start', run=run_id, method=method, models=list(models), prompt_version=prompt_version,
                  priority=list(resolver.entries), priority_unresolved=resolver.unresolved(),
                  settings=settings, dates_total=dates_total, dates_ai=dates_ai)

    def log_run_end(self, *, run_id, method, status, error, stats, models_used, seconds) -> None:
        self.emit('run_end', run=run_id, method=method, status=status, error=error or None, stats=stats,
                  models_used=models_used, seconds=round(seconds, 1))

    def log_bad_output(self, *, run_id, day, error, raw, model) -> None:
        self.emit('bad_output', run=run_id, day=day, model=model, error=str(error), raw=(raw or '')[:RAW_LIMIT])

    def log_error(self, event: str, *, run_id, day, error) -> None:
        self.emit(event, run=run_id, day=day, error=error)

    # ------------------------------------------------------------------ одна дата

    def log_day(self, plan, cands: list[Record], resolver, *, run_id, model, groups, asked_ai: bool,
                prev: list[dict] | None = None) -> None:
        """Все события одной даты. Вызывать после успешного применения плана."""
        day = plan.day.isoformat()
        wanted = {nd.loser.key: nd for nd in plan.wanted}
        inserted = {nd.loser.key for nd in plan.insert}
        member_group: dict[tuple, str] = {}
        winner_of_group: dict[str, tuple | None] = {}
        group_rows = []
        for gi, g in enumerate(plan.groups, 1):
            gid = f"{day}#{gi}"
            wkey = plan.group_winners[gi - 1] if gi - 1 < len(plan.group_winners) else None
            winner = next((m for m in g.members if m.key == wkey), None) if wkey else None
            winner_of_group[gid] = winner.key if winner else None
            members = []
            for m in sorted(g.members, key=resolver.rank):
                member_group.setdefault(m.key, gid)
                if winner is not None and m.key == winner.key:
                    members.append(self._rec(m, resolver, role='winner'))
                elif m.key in wanted:
                    members.append(self._rec(m, resolver, role='loser', via=wanted[m.key].via))
                else:
                    members.append(self._rec(m, resolver, role='kept', reason=_kept_reason(m, plan, winner)))
            if winner is not None:
                members.sort(key=lambda x: 0 if x['role'] == 'winner' else 1 if x['role'] == 'loser' else 2)
            group_rows.append((gid, g, members, winner))

        sources = {r.source for r in cands}
        self.emit('day', run=run_id, day=day, method=plan.method, asked_ai=asked_ai, model=model,
                  records=len(cands), sources=len(sources), groups=len(plan.groups),
                  lists=len(plan.lists), weak=len(plan.weak), decisions=len(plan.wanted),
                  unchanged_decisions=plan.kept, sticky=len(plan.sticky), hide=len(plan.hide), unhide=len(plan.unhide))

        if self.raw_answers and getattr(groups, 'raw', None):
            self.emit('ai_raw', run=run_id, day=day, models=list(groups.models),
                      chunks=[x[:RAW_LIMIT] for x in groups.raw])

        for gid, g, members, winner in group_rows:
            self.emit('group', run=run_id, day=day, group=gid, model=model, reason=g.reason,
                      winner=({'kind': winner.kind, 'id': winner.id} if winner else None), members=members)

        prev_by_loser = {(p['loser_kind'], p['loser_id']): p for p in (prev or [])}
        for nd in plan.wanted:
            old = prev_by_loser.get(nd.loser.key)
            status = ('proposed' if plan.method != 'ai'
                      else 'active_new' if nd.loser.key in inserted else 'active_existing')
            self.emit('decision', run=run_id, day=day, group=member_group.get(nd.loser.key), via=nd.via,
                      status=status, is_new=old is None, model=model, reason=nd.reason,
                      winner=self._rec(nd.winner, resolver), loser=self._rec(nd.loser, resolver))

        kept_done: set = set()
        for gid, g, members, winner in group_rows:
            for m in g.members:
                if m.key in wanted or (winner is not None and m.key == winner.key) or m.key in kept_done:
                    continue
                kept_done.add(m.key)
                self.emit('kept', run=run_id, day=day, group=gid, reason=_kept_reason(m, plan, winner),
                          record=self._rec(m, resolver))
        for r in cands:
            if r.key in kept_done or r.key in wanted:
                continue
            if r.is_digest or r.is_list or r.key in plan.lists:
                kept_done.add(r.key)
                self.emit('kept', run=run_id, day=day, group=member_group.get(r.key),
                          reason=_kept_reason(r, plan, None), record=self._rec(r, resolver))

        recs = {r.key: r for r in cands}
        for old_w, new_w in plan.merged.items():
            self.emit('merge', run=run_id, day=day, previous_winner=self._rec(recs[old_w], resolver) if old_w in recs else None,
                      winner=self._rec(recs[new_w], resolver) if new_w in recs else None)
        sticky = {d.loser: d for d in plan.sticky}
        for lk, d in sticky.items():                      # AI не повторил группу, решение оставлено
            self.emit('sticky', run=run_id, day=day, reason=d.reason, decision_id=d.id,
                      winner=self._rec(recs[d.winner], resolver) if d.winner in recs else None,
                      loser=self._rec(recs[lk], resolver) if lk in recs else None)
        for p in prev or []:
            if p['status'] == 'reverted' or p['decided_by'] == 'admin':
                continue
            lk, wk = (p['loser_kind'], p['loser_id']), (p['winner_kind'], p['winner_id'])
            if lk in sticky:
                continue
            nd = wanted.get(lk)
            if nd is not None and nd.winner.key == wk:
                continue
            self.emit('change', run=run_id, day=day, change='winner_changed' if nd is not None else 'dropped',
                      previous={'winner': {'kind': wk[0], 'id': wk[1]}, 'loser': {'kind': lk[0], 'id': lk[1]},
                                'status': p['status'], 'model': p.get('model'), 'reason': p.get('reason')},
                      loser=self._rec(recs[lk], resolver) if lk in recs else None,
                      new_winner=self._rec(nd.winner, resolver) if nd is not None else None)


def _default(o):
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    if isinstance(o, (set, frozenset, tuple)):
        return list(o)
    return str(o)


def _link(r: Record, resolver) -> str | None:
    if r.kind == KIND_EVENT:
        return r.url
    if r.tg_id is None or not r.source.startswith('ch:'):
        return None
    cid = int(r.source[3:])
    name = resolver.channel_names.get(cid) if resolver else None
    return f"https://t.me/{name.lstrip('@')}/{r.tg_id}" if name else f"https://t.me/c/{cid}/{r.tg_id}"


def _kept_reason(m: Record, plan, winner: Record | None) -> str:
    if m.is_digest:
        return 'digest'
    if m.is_list or m.key in plan.lists:
        return 'list'
    if m.key in plan.locked:
        return 'admin_locked'
    if m.key in plan.pinned:
        return 'reverted_by_admin'
    if winner is None:
        return 'no_winner'
    if m.source == winner.source:
        return 'same_source'
    if m.key in plan.weak:
        return 'weak'
    return 'other'


_CACHE: dict[str, AuditLog] = {}
_NULL = NullAudit()


def audit_from_cfg(cfg: dict | None, path: Path | str | None = None):
    """Журнал по секции deduplication: выключен, если секции audit нет или enabled=false."""
    a = (cfg or {}).get('audit') or {}
    if not a.get('enabled'):
        return _NULL
    p = Path(path) if path else default_path()
    key = str(p)
    if key not in _CACHE:
        try:
            _CACHE[key] = AuditLog(p, max_text_chars=a.get('max_text_chars', 4000), rotation_mb=a.get('rotation_mb', 50),
                                   retention_days=a.get('retention_days', 90), raw_answers=a.get('raw_answers', True))
        except Exception as e:
            logger.error(f"DEDUP_AUDIT_INIT_ERROR | {type(e).__name__}: {e} — журнал отключён")
            return _NULL
    return _CACHE[key]
