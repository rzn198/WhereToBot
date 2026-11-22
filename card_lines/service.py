"""Генерация ИИ-строк списка категорий после работы дедупликатора.

Запускается из сервиса дедупликатора (deduplicator/main.py) после УСПЕШНОГО прогона и один раз при старте сервиса,
как перевод описаний. Состояние не хранится: «что считать» каждый раз выводится из БД (card_lines.select),
поэтому повторный запуск безопасен. card_lines.method: off | shadow (только лог «сколько записей и по каким профилям») | on.
Любой сбой ИИ оставляет запись без строки: бот покажет обычную строку из данных БД.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone

from loguru import logger

from card_lines.enrich import DbCache, Enricher, EnrichUnavailable
from card_lines.select import select_candidates
from card_lines.settings import CardLineSettings
from card_lines.text import (PROMPT_VERSION, SYSTEM_PROMPT, BadCardLine, Candidate, Parts, build_batch_prompt,
                             build_user_prompt, hook_wanted, parse_batch, parse_single, useful_chars)
from db import card_lines as store
from shared.ai import AIUnavailable
from shared.tz import belgrade_today

DEFAULT_CITY = 'Belgrade'     # как bot.handlers.events.EVENTS_CITY: бот показывает только события Белграда
DEFAULT_HORIZON_DAYS = 365    # как bot.utils.categories.DEFAULT_HORIZON_DAYS
BAD_STATUSES = ('incomplete', 'error', 'skipped')
MAX_FAILS = 2          # столько раз подряд запись может не пройти проверку, потом её пропускаем до перезапуска сервиса


@dataclass
class PassResult:
    status: str = 'ok'          # ok | partial (упёрлись в max_calls_per_run) | incomplete | error | skipped | off
    candidates: int = 0
    generated: int = 0
    failed: int = 0             # ответ не прошёл проверку
    no_value: int = 0           # название принято, значение атрибута пусто (модель не уверена / не из списка)
    remaining: int = 0
    calls: int = 0
    copied: int = 0             # взяли готовую строку у записи с тем же хэшем (без запроса)
    hooks: Counter = field(default_factory=Counter)     # статусы хука: ok, ok_web, none, invalid, not_found, retry
    error: str = ''
    by_profile: Counter = field(default_factory=Counter)


def config_horizon_days() -> int:
    """Как далеко вперёд показывает бот: config cleanup.events_max_future_days."""
    try:
        from shared.config import config
        return int((config.get('cleanup') or {}).get('events_max_future_days', DEFAULT_HORIZON_DAYS))
    except (TypeError, ValueError):
        return DEFAULT_HORIZON_DAYS


async def generate(client, chunk: list[Candidate], st: CardLineSettings) -> list[Parts | BadCardLine]:
    """Один запрос: одна запись или пакет записей ОДНОГО профиля. Результат по порядку chunk. AIUnavailable пробрасывается."""
    if len(chunk) == 1:
        raw = await client.complete(SYSTEM_PROMPT, build_user_prompt(chunk[0], st.input_chars, st.title_max, st.hooks))
        try:
            return [parse_single(raw, chunk[0], st.input_chars, st.title_max, st.hooks)]
        except BadCardLine as e:
            return [e]
    raw = await client.complete(SYSTEM_PROMPT, build_batch_prompt(chunk, st.input_chars, st.title_max, st.hooks))
    try:
        return parse_batch(raw, chunk, st.input_chars, st.title_max, st.hooks)
    except BadCardLine as e:
        return [e] * len(chunk)


async def finish_hook(client, c: Candidate, parts: Parts, st: CardLineSettings, enricher) -> tuple[Parts, str | None, str, int]:
    """Доводит хук записи: (parts, hook_status, hook_url, запросов к модели).
    hook_status: None хуки не просили | ok | none (данных не хватило) | invalid (не прошёл проверку) |
                 not_found (интернет ничего не нашёл) | retry (сбой сети или моделей: повторим позже).
    Хук по короткому описанию не принимается (основание только в названии): сразу идём в интернет.
    Второй запрос к модели делается только с найденными фактами; повод и программу по фактам из интернета код не пропустит."""
    hs = st.hooks
    if not hook_wanted(c.profile, hs):
        return parts, None, '', 0
    if useful_chars(c.body) < hs.min_description_chars and parts.hook is not None:
        parts = replace(parts, hook=None, hook_status='none')
    if parts.hook is not None:
        return parts, 'ok', '', 0
    if parts.hook_status == 'invalid':
        return parts, 'invalid', '', 0
    if not (hs.enrich.enabled and enricher is not None and c.profile.enrich):
        return parts, 'none', '', 0
    try:
        facts = await enricher.facts_for(c.profile.enrich, parts.title, hint=f"{c.title}\n{(c.body or '')[:st.input_chars]}")
    except EnrichUnavailable:
        return parts, 'retry', '', 0
    if facts is None:
        return parts, 'not_found', '', 0
    try:
        raw = await client.complete(SYSTEM_PROMPT, build_user_prompt(c, st.input_chars, st.title_max, hs, facts.text, facts.source))
    except AIUnavailable:
        return parts, 'retry', '', 1
    try:
        p2 = parse_single(raw, c, st.input_chars, st.title_max, hs, facts.text)
    except BadCardLine as e:
        return replace(parts, hook_error=f"второй запрос: {e}"), 'invalid', '', 1
    if p2.hook is not None:
        return (replace(parts, hook=p2.hook, hook_status='ok', enough=p2.enough, text_time=parts.text_time or p2.text_time),
                'ok', facts.url if p2.hook.source == 'web' else '', 1)
    return (replace(parts, hook_error=p2.hook_error) if p2.hook_status == 'invalid' else parts,
            ('invalid' if p2.hook_status == 'invalid' else 'none'), '', 1)


def make_chunks(todo: list[Candidate], batch_size: int) -> list[list[Candidate]]:
    """Пакеты по профилю (в одном запросе одни правила), порядок по датам сохраняется."""
    if batch_size <= 1:
        return [[c] for c in todo]
    open_: dict[str, list[Candidate]] = {}
    out: list[list[Candidate]] = []
    for c in todo:
        buf = open_.setdefault(c.profile.name, [])
        buf.append(c)
        if len(buf) >= batch_size:
            out.append(buf)
            open_[c.profile.name] = []
    out += [b for b in open_.values() if b]
    return out


async def run_pass(pool, client, st: CardLineSettings, *, city: str | None = None, horizon_days: int | None = None,
                   today=None, fails: Counter | None = None, enricher=None) -> PassResult:
    res = PassResult()
    if not st.enabled:
        res.status = 'off'
        return res
    city = city or DEFAULT_CITY
    if horizon_days is None:
        horizon_days = config_horizon_days()
    fails = fails if fails is not None else Counter()

    sel = await select_candidates(pool, st, today=today or belgrade_today(), horizon_days=horizon_days, city=city)
    todo = [c for c in sel.todo if fails[c.key] < MAX_FAILS]
    res.candidates = len(todo)
    res.by_profile = Counter(c.profile.name for c in todo)
    if not todo:
        logger.info(f"CARD_LINES | нечего считать | всего={sel.total} | актуальных={sel.up_to_date} | без_профиля={sel.no_profile}")
        return res
    if st.method == 'shadow':
        logger.info(f"CARD_LINES_SHADOW | к генерации={len(todo)} | профили={dict(res.by_profile)} | "
                    f"актуальных={sel.up_to_date} (запросы к модели не отправляются, строки не пишутся)")
        return res
    if not client.configured:
        res.status = 'skipped'
        res.error = "нет OPENCODE_CARDLINES_API_KEY или card_lines.models"
        logger.warning(f"CARD_LINES_SKIP | {res.error}")
        return res

    copied = await store.copy_by_hash(pool, todo, PROMPT_VERSION)
    if copied:
        res.copied = len(copied)
        todo = [c for c in todo if c.key not in copied]
        logger.info(f"CARD_LINES_COPY | скопировано по хэшу (повторы события)={len(copied)}")
    if not todo:
        return res
    if enricher is None and st.hooks.enabled and st.hooks.enrich.enabled:
        enricher = Enricher(st.hooks.enrich, DbCache(pool))
        own_enricher = True
    else:
        own_enricher = False
    client.begin_run()
    chunks = make_chunks(todo, st.batch_size)
    logger.info(f"CARD_LINES_START | записей={len(todo)} | запросов≈{len(chunks)} | batch_size={st.batch_size} | "
                f"моделей={len(client.models)} | профили={dict(res.by_profile)}")
    sem = asyncio.Semaphore(st.concurrency)
    stop = asyncio.Event()

    async def one(chunk: list[Candidate]) -> None:
        async with sem:
            if stop.is_set():
                res.remaining += len(chunk)
                return
            if res.calls >= st.max_calls_per_run:
                res.remaining += len(chunk)
                res.status = 'partial' if res.status == 'ok' else res.status
                return
            res.calls += 1
            try:
                parts = await generate(client, chunk, st)
            except AIUnavailable as e:
                stop.set()
                res.remaining += len(chunk)
                res.error = res.error or str(e)
                logger.warning(f"CARD_LINES_STOP | {e}")
                return
        good = 0
        for c, p in zip(chunk, parts):
            if isinstance(p, BadCardLine):
                res.failed += 1
                fails[c.key] += 1
                logger.warning(f"CARD_LINE_BAD | {c.kind}{c.id} | {c.profile.name} | {p}")
                continue
            good += 1
            fails.pop(c.key, None)
            p, hstatus, hurl, extra_calls = await finish_hook(client, c, p, st, enricher)
            res.calls += extra_calls
            model = client.current_model
            if hstatus:
                res.hooks['ok_web' if hstatus == 'ok' and p.hook and p.hook.source == 'web' else hstatus] += 1
            try:
                await store.save_line(pool, c.kind, c.id, p.title, p.value, c.profile.name, c.src_hash, model, PROMPT_VERSION,
                                      hook=p.hook, hook_status=hstatus, hook_url=hurl, text_time=p.text_time)
            except Exception as e:
                res.failed += 1
                logger.error(f"CARD_LINE_SAVE_FAILED | {c.kind}{c.id} | {type(e).__name__}: {e}")
                continue
            res.generated += 1
            if p.value is None:
                res.no_value += 1
            logger.info(f"CARD_LINE | {c.kind}{c.id} | {c.profile.name} | {p.title!r} | {p.value or '-'} | {model}")
            if hstatus:
                logger.info(f"CARD_HOOK | {c.kind}{c.id} | {hstatus} | f{p.hook.formula if p.hook else '-'} | "
                            f"{p.hook.source if p.hook else '-'} | {p.hook.text if p.hook else p.hook_error or '-'}")
        client.report_ok() if good else client.report_bad_output()

    await asyncio.gather(*[one(c) for c in chunks])
    if stop.is_set():
        res.status = 'incomplete'
    await client.aclose()
    if own_enricher:
        logger.info(f"CARD_ENRICH | {enricher.stats}")
        await enricher.aclose()
    logger.info(f"CARD_LINES_DONE | status={res.status} | записей={res.candidates} | готово={res.generated} | "
                f"без_значения={res.no_value} | не_прошли_проверку={res.failed} | осталось={res.remaining} | хуки={dict(res.hooks)} | "
                f"запросов={res.calls} | {client.stats.line()}")
    return res


class CardLinesHook:
    """Запуск из сервиса дедупликатора: не блокирует его цикл, два прохода одновременно не идут."""

    def __init__(self, pool, client, st: CardLineSettings, send=None):
        self.pool, self.client, self.st, self.send = pool, client, st, send
        self._bad = False
        self._exhausted_sent = False
        self._task: asyncio.Task | None = None
        self._again = False
        self.last: PassResult | None = None
        self.last_finish: datetime | None = None
        self.retries = 0
        self.fails: Counter = Counter()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def trigger(self, reason: str) -> bool:
        if not self.st.enabled:
            return False
        if self.running:
            self._again = True
            logger.info(f"CARD_LINES_BUSY | reason={reason} | повторим после текущего прохода")
            return False
        logger.info(f"CARD_LINES_TRIGGER | reason={reason} | method={self.st.method}")
        self._task = asyncio.ensure_future(self._run())
        return True

    async def _run(self) -> None:
        try:
            self.last = await run_pass(self.pool, self.client, self.st, fails=self.fails)
        except Exception as e:                      # генерация не должна ронять дедупликатор
            logger.exception(f"CARD_LINES_ERROR | {type(e).__name__}: {e}")
            self.last = PassResult(status='error', error=f"{type(e).__name__}: {e}")
        self.last_finish = datetime.now(timezone.utc)
        if self.last.status in ('ok', 'partial'):
            self.retries = 0
        await self._alert(self.last)
        if self._again:
            self._again = False
            self._task = None
            self.trigger('queued')

    async def _alert(self, res: PassResult) -> None:
        if self.send is None or not self.st.alerts or self.st.method != 'on':
            return
        text = None
        if res.status in BAD_STATUSES:
            if not self._bad:
                self._bad = True
                again = (f"Повтор через {self.st.retry_minutes:g} мин, до {self.st.max_retries} раз подряд."
                         if res.status != 'skipped' and self.st.max_retries else "Автоповтор не предусмотрен: исправь настройку и перезапусти сервис.")
                text = (f"🔴 ИИ-строки списка не сформированы ({res.status}): {res.error or 'причина не указана'}\n"
                        f"Записей без строки: {res.remaining or res.candidates}. Бот показывает обычные строки. {again}")
            elif (res.status != 'skipped' and self.st.max_retries and self.retries >= self.st.max_retries
                  and not self._exhausted_sent):
                self._exhausted_sent = True
                text = "🔴 ИИ-строки списка: повторы исчерпаны, модели по-прежнему недоступны. Следующая попытка — после следующего прогона дедупликатора или перезапуска сервиса."
        elif res.status in ('ok', 'partial') and self._bad:
            self._bad = self._exhausted_sent = False
            text = f"🟢 ИИ-строки списка снова формируются (готово {res.generated})"
        if text:
            try:
                await self.send(text)
            except Exception as e:
                logger.error(f"CARD_LINES_ALERT_ERROR | {type(e).__name__}: {e}")

    def poll(self, now: datetime | None = None) -> bool:
        """Повтор после неудачи (через retry_minutes, не больше max_retries раз подряд) и продолжение после max_calls_per_run."""
        now = now or datetime.now(timezone.utc)
        if self.running or self.last is None or self.last_finish is None:
            return False
        if self.last.status == 'partial' and now - self.last_finish >= timedelta(seconds=self.st.continue_seconds):
            return self.trigger('continue')
        if (self.last.status in ('incomplete', 'error') and self.retries < self.st.max_retries
                and now - self.last_finish >= timedelta(minutes=self.st.retry_minutes)):
            self.retries += 1
            return self.trigger(f"retry {self.retries}/{self.st.max_retries}")
        return False

    async def aclose(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
