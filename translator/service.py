"""Перевод описаний событий (events.description -> events.description_ru) после работы дедупликатора.

Запускается из сервиса дедупликатора (deduplicator/main.py) сразу после УСПЕШНОГО прогона и один раз при старте
сервиса. Переводятся только события-победители из списка translation.sources (см. translator/select.py).
Состояние не хранится: «что переводить» каждый раз выводится из БД, поэтому повторный запуск безопасен.

translation.method: off — не работает | shadow — только лог «что было бы переведено» | on — переводит.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from loguru import logger

from db.tables import Tables
from shared.ai import AIUnavailable
from translator.select import select_candidates
from translator.text import SYSTEM_PROMPT, BadTranslation, build_description_ru, cut_input, is_russian, validate

METHODS = ('off', 'shadow', 'on')


@dataclass(frozen=True)
class TranslationSettings:
    method: str = 'off'
    sources: tuple[str, ...] = ()
    max_chars: int = 600              # длина description_ru до ссылки на билеты
    input_chars: int = 1500           # сколько символов оригинала отправляем модели
    concurrency: int = 3
    only_upcoming: bool = True
    retry_minutes: float = 30
    max_retries: int = 3
    run_on_start: bool = True

    @property
    def enabled(self) -> bool:
        return self.method in ('shadow', 'on') and bool(self.sources)


def translation_settings(cfg: dict) -> TranslationSettings:
    c = cfg or {}
    method = str(c.get('method') or 'off').strip().lower()
    d = TranslationSettings()
    return TranslationSettings(
        method=method if method in METHODS else 'off',
        sources=tuple(str(s).strip().lower() for s in (c.get('sources') or []) if str(s).strip()),
        max_chars=max(50, int(c.get('max_chars', d.max_chars))),
        input_chars=max(200, int(c.get('input_chars', d.input_chars))),
        concurrency=max(1, int(c.get('concurrency', d.concurrency))),
        only_upcoming=bool(c.get('only_upcoming', d.only_upcoming)),
        retry_minutes=max(1.0, float(c.get('retry_minutes', d.retry_minutes))),
        max_retries=max(0, int(c.get('max_retries', d.max_retries))),
        run_on_start=bool(c.get('run_on_start', d.run_on_start)),
    )


@dataclass
class PassResult:
    status: str = 'ok'          # ok | incomplete (модели исчерпаны/недоступны) | off | skipped
    candidates: int = 0
    translated: int = 0         # переведено моделью
    copied_ru: int = 0          # уже русские: только добавлена ссылка
    failed: int = 0             # ответ модели не прошёл проверку (останется на следующий раз)
    stale: int = 0              # описание изменилось во время перевода — результат не сохранён
    remaining: int = 0


async def run_pass(pool, client, st: TranslationSettings, *, today=None) -> PassResult:
    """Один проход: выбрать победителей, перевести, сохранить."""
    res = PassResult()
    if not st.enabled:
        res.status = 'off'
        return res
    rows = await select_candidates(pool, list(st.sources), only_upcoming=st.only_upcoming, today=today)
    res.candidates = len(rows)
    if not rows:
        logger.info("TRANSLATE | нет событий для перевода")
        return res

    if st.method == 'shadow':
        by_source = Counter(r['source'] for r in rows)
        russian = sum(1 for r in rows if is_russian(r['description']))
        logger.info(f"TRANSLATE_SHADOW | кандидатов={len(rows)} | уже на русском (без модели)={russian} | "
                    f"к переводу моделью={len(rows) - russian} | источники={dict(by_source)}")
        res.status = 'ok'
        return res

    if not client.configured:
        logger.warning("TRANSLATE_SKIP | нет OPENCODE_API_KEY или translation.models")
        res.status = 'skipped'
        return res

    client.begin_run()
    logger.info(f"TRANSLATE_START | кандидатов={len(rows)} | моделей={len(client.models)}")
    sem = asyncio.Semaphore(st.concurrency)
    stop = asyncio.Event()

    async def save(row, translated: str, source_text: str) -> None:
        desc_ru = build_description_ru(translated, row['source_url'], st.max_chars)
        updated = await pool.execute(
            f"UPDATE {Tables.EVENTS} SET description_ru = $1 "
            f"WHERE id = $2 AND description_ru IS NULL AND description IS NOT DISTINCT FROM $3",
            desc_ru, row['id'], row['description'])
        if updated.endswith(' 0'):
            res.stale += 1

    async def one(row) -> None:
        text = row['description']
        if is_russian(text):
            await save(row, text, text)
            res.copied_ru += 1
            return
        async with sem:
            if stop.is_set():
                res.remaining += 1
                return
            sent = cut_input(text, st.input_chars)
            try:
                out = await client.complete(SYSTEM_PROMPT, sent)
                validate(out, sent)
            except AIUnavailable as e:
                stop.set()
                res.remaining += 1
                logger.warning(f"TRANSLATE_STOP | {e}")
                return
            except BadTranslation as e:
                client.report_bad_output()
                res.failed += 1
                logger.warning(f"TRANSLATE_BAD | id={row['id']} | {e}")
                return
        client.report_ok()
        await save(row, out, sent)
        res.translated += 1

    await asyncio.gather(*[one(r) for r in rows])
    if stop.is_set():
        res.status = 'incomplete'
    logger.info(f"TRANSLATE_DONE | status={res.status} | кандидатов={res.candidates} | переведено={res.translated} | "
                f"уже_на_русском={res.copied_ru} | не_прошли_проверку={res.failed} | устарело={res.stale} | "
                f"осталось={res.remaining} | {client.stats.line()}")
    return res


class TranslationHook:
    """Запуск перевода из сервиса дедупликатора: не блокирует его цикл, два прохода одновременно не идут."""

    def __init__(self, pool, client, st: TranslationSettings):
        self.pool, self.client, self.st = pool, client, st
        self._task: asyncio.Task | None = None
        self._again = False
        self.last: PassResult | None = None
        self.last_finish: datetime | None = None
        self.retries = 0

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def trigger(self, reason: str) -> bool:
        if not self.st.enabled:
            return False
        if self.running:
            self._again = True                      # после текущего прохода будет ещё один: данные успели измениться
            logger.info(f"TRANSLATE_BUSY | reason={reason} | повторим после текущего прохода")
            return False
        logger.info(f"TRANSLATE_TRIGGER | reason={reason} | method={self.st.method}")
        self._task = asyncio.ensure_future(self._run())
        return True

    async def _run(self) -> None:
        try:
            self.last = await run_pass(self.pool, self.client, self.st)
        except Exception as e:                      # перевод не должен ронять дедупликатор
            logger.exception(f"TRANSLATE_ERROR | {type(e).__name__}: {e}")
            self.last = PassResult(status='error')
        self.last_finish = datetime.now(timezone.utc)
        if self.last.status == 'ok':
            self.retries = 0
        if self._again:
            self._again = False
            self._task = None
            self.trigger('queued')

    def poll(self, now: datetime | None = None) -> bool:
        """Повтор после неудачи (модели недоступны): через retry_minutes, не больше max_retries раз подряд."""
        now = now or datetime.now(timezone.utc)
        if (not self.running and self.last is not None and self.last.status in ('incomplete', 'error')
                and self.retries < self.st.max_retries and self.last_finish is not None
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
