"""Прогон дедупликации: даты → AI → план → применение.

method:
  legacy — не используется здесь (работает старый хук);
  shadow — AI предлагает группы, решения пишутся как 'proposed', ничего не скрывается;
  ai     — проигравшие скрываются (hidden_by_admin), устаревшие решения снимаются.
dry_run=True — вообще ничего не пишем в БД (скрипт dedup_replay.py).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from datetime import date, timedelta

import asyncpg
from loguru import logger

from deduplicator import store
from deduplicator.engine import DayPlan, Live, candidates, plan_day
from deduplicator.grouping import (PROMPT_VERSION, SYSTEM_PROMPT, BadGroupingOutput, Groups, build_prompt,
                                   chunk_records, parse_groups)
from deduplicator.priority import PriorityResolver
from deduplicator.records import Record, fingerprint
from shared.ai import AIClient, AIUnavailable

METHODS = ('legacy', 'shadow', 'ai')


@dataclass
class RunResult:
    status: str = 'ok'                    # ok | skipped (AI недоступен) | error
    error: str = ''
    run_id: int | None = None
    plans: list[DayPlan] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    resolver: PriorityResolver | None = None


def ai_settings(cfg: dict) -> dict:
    ai = (cfg or {}).get('ai') or {}
    return dict(
        priority=list(ai.get('priority') or []),
        max_records=int(ai.get('max_records_per_request', 80)),
        max_chars=int(ai.get('max_chars_per_record', 300)),
        concurrency=max(1, int(ai.get('concurrency', 3))),
        max_requests=int(ai.get('max_requests_per_run', 300)),
        horizon_days=int(ai.get('horizon_days', 365)),
    )


def _after_apply(cands: list[Record], plan: DayPlan) -> list[Record]:
    """Записи даты в том виде, какой они примут после применения плана. Отпечаток сохраняем именно по ним:
    иначе собственные скрытия меняли бы отпечаток, и следующий прогон зря заново спрашивал бы AI."""
    if plan.method != 'ai':
        return cands
    hide, unhide = set(plan.hide), set(plan.unhide)
    return [replace(r, hidden=True) if r.key in hide else replace(r, hidden=False) if r.key in unhide else r
            for r in cands]


async def _ask_day(client: AIClient, recs: list[Record], resolver: PriorityResolver, s: dict) -> Groups:
    ordered = sorted(recs, key=lambda r: (resolver.label(r), r.id))
    labels = {r.key: resolver.label(r) for r in recs}
    used: set = set()
    out = Groups()
    for chunk in chunk_records(ordered, s['max_records']):
        prompt, index = build_prompt(chunk, labels, s['max_chars'])
        raw = await client.complete(SYSTEM_PROMPT, prompt)
        try:
            groups = parse_groups(raw, index)
        except BadGroupingOutput as e:
            client.report_bad_output()
            logger.warning(f"DEDUP_BAD_OUTPUT | day={recs[0].day} | {e} | raw={raw[:200]!r}")
            raise
        client.report_ok()
        out.lists |= groups.lists
        out.weak |= groups.weak
        for g in groups:
            if any(m.key in used for m in g.members):
                continue
            used.update(m.key for m in g.members)
            out.append(g)
    return out


async def run_dedup(pool: asyncpg.Pool, client: AIClient, cfg: dict, *, method: str, dry_run: bool = False,
                    force: bool = False, only_days: list[date] | None = None, start_date: date | None = None,
                    ignore_hidden: bool = False, max_days: int | None = None) -> RunResult:
    """start_date/ignore_hidden/max_days — для dedup_replay.py: анализ прошлых дат, скрытые записи считаются
    видимыми (чтобы сравнить с прежним хуком), ограничение числа дат."""
    if method not in ('shadow', 'ai'):
        raise ValueError(f"run_dedup: method должен быть shadow или ai, получено {method!r}")
    s = ai_settings(cfg)
    res = RunResult(stats=dict(dates_total=0, dates_processed=0, requests=0, tokens_in=0, tokens_out=0,
                               groups_found=0, hidden=0, unhidden=0, bad_days=0, skipped_days=0))
    st = res.stats
    client.begin_run()
    if not client.available:
        res.status, res.error = 'skipped', 'AI недоступен: нет ключа/моделей или модели исчерпаны'
        return res

    today = start_date or await pool.fetchval("SELECT CURRENT_DATE")
    end = (await pool.fetchval("SELECT CURRENT_DATE") if start_date else today) + timedelta(days=s['horizon_days'])
    names = await store.load_channel_names(pool)
    resolver = PriorityResolver(s['priority'], names)
    resolver.warn_unresolved()
    res.resolver = resolver

    records = await store.load_records(pool, today, end)
    if ignore_hidden:
        records = [replace(r, hidden=False) for r in records]
    live_by_day = await store.load_live(pool, today, end)
    stored_fp = {} if force else await store.load_fingerprints(pool, today, end)
    by_day: dict[date, list[Record]] = {}
    for r in records:
        by_day.setdefault(r.day, []).append(r)

    salt = f"{method}|{PROMPT_VERSION}|{'>'.join(resolver.entries)}|{s['max_chars']}|{s['max_records']}"
    work: list[tuple[date, list[Record], list[Live], str, bool]] = []
    for day in sorted(set(by_day) | set(live_by_day)):
        if only_days and day not in only_days:
            continue
        live = live_by_day.get(day, [])
        cands = candidates(by_day.get(day, []), live)
        fp = fingerprint(cands, salt)
        if stored_fp.get(day) == fp:
            continue
        need_ai = len({r.source for r in cands}) >= 2
        work.append((day, cands, live, fp, need_ai))
    if max_days:
        work = work[:max_days]
    st['dates_total'] = len(work)

    if not dry_run:
        if method == 'ai' and not await store.had_ai_run(pool):
            await store.release_legacy_marks(pool)
        res.run_id = await store.start_run(pool, method)

    stats0 = (client.stats.calls, client.stats.prompt_tokens, client.stats.completion_tokens)
    budget = {'left': s['max_requests']}
    sem = asyncio.Semaphore(s['concurrency'])
    down: dict = {'cause': None}
    try:
        async def ask(day, cands):
            async with sem:
                if down['cause']:
                    return None
                n_req = len(chunk_records(cands, s['max_records']))
                if budget['left'] < n_req:
                    st['skipped_days'] += 1
                    return None
                budget['left'] -= n_req
                try:
                    return await _ask_day(client, cands, resolver, s)
                except AIUnavailable as e:
                    down['cause'] = str(e)
                    return None
                except BadGroupingOutput:
                    st['bad_days'] += 1
                    return None
                except Exception as e:                # неожиданный сбой одной даты не должен ронять весь прогон
                    logger.error(f"DEDUP_DAY_ERROR | day={cands[0].day} | {type(e).__name__}: {e}")
                    st['bad_days'] += 1
                    return None

        asked = {day: asyncio.ensure_future(ask(day, cands)) for day, cands, _, _, need in work if need}
        await asyncio.gather(*asked.values())

        model = getattr(client, 'current_model', '') or ''
        for day, cands, live, fp, need_ai in work:
            if need_ai:
                groups = asked[day].result()
                if groups is None:                    # не спрашивали или ответ плохой — дату повторим в следующий раз
                    continue
            else:
                groups = Groups()
            plan = plan_day(day, cands, groups, live, resolver, method)
            res.plans.append(plan)
            st['dates_processed'] += 1
            st['groups_found'] += len(groups)
            st['hidden'] += len(plan.hide)
            st['unhidden'] += len(plan.unhide)
            if not dry_run:
                try:
                    await store.apply_day(pool, plan, run_id=res.run_id, model=model,
                                          fingerprint=fingerprint(_after_apply(cands, plan), salt))
                except Exception as e:
                    logger.error(f"DEDUP_APPLY_ERROR | day={day} | {type(e).__name__}: {e}")
                    st['dates_processed'] -= 1
                    st['hidden'] -= len(plan.hide)
                    st['unhidden'] -= len(plan.unhide)
                    res.plans.pop()

    except Exception as e:
        logger.exception(f"DEDUP_RUN_ERROR | {type(e).__name__}: {e}")
        res.status, res.error = 'error', f"{type(e).__name__}: {e}"
    st['requests'] = client.stats.calls - stats0[0]
    st['tokens_in'] = client.stats.prompt_tokens - stats0[1]
    st['tokens_out'] = client.stats.completion_tokens - stats0[2]
    if down['cause']:
        res.status, res.error = 'skipped', f"AI недоступен: {down['cause']}"
    if not dry_run and res.run_id:
        await store.finish_run(pool, res.run_id, res.status, st, res.error)
    logger.info(f"DEDUP_RUN | method={method} dry={dry_run} status={res.status} " +
                ' '.join(f"{k}={v}" for k, v in st.items()))
    return res
