"""AI киноафиши: один проход = (A) названия и склейка -> жанры -> (B) перевод описаний.

Что делать, выводится из БД каждый раз (состояния в памяти нет), поэтому повторный запуск безопасен:
  * этап A — фильмы с будущими сеансами, у которых нет ответа AI текущей версии промпта (TITLES_VERSION);
  * жанры — только метки, которых ещё нет в cinema_genre_map (один вызов на все новые);
  * этап B — фильмы с описанием без перевода, либо если описание выросло в retranslate_ratio раз.
Расход ограничен: не больше max_films_per_run фильмов на этап, фильм с max_fails неудачами подряд пропускается.

apply=False («тень»): модель вызывается, но в БД ничего не пишется — результаты возвращаются в collect для CSV.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from loguru import logger

from cinema_ai import genres as G
from cinema_ai import store
from cinema_ai import titles as T
from cinema_ai.settings import CinemaAISettings
from shared.ai import AIUnavailable
from translator.text import SYSTEM_PROMPT as TRANSLATE_PROMPT, BadTranslation, cut_input, is_russian, validate

GENRE_CHUNK = 40


@dataclass
class PassResult:
    status: str = 'ok'              # ok | incomplete (модели исчерпаны/недоступны) | skipped (нет ключа) | off
    films: int = 0                  # фильмов с будущими сеансами
    titles_todo: int = 0
    titles_done: int = 0
    titles_failed: int = 0
    merges: int = 0
    merge_vetoes: int = 0
    genre_terms_new: int = 0
    desc_todo: int = 0
    translated: int = 0
    copied_ru: int = 0              # описание уже русское — скопировано без модели
    trans_failed: int = 0
    stale: int = 0
    error: str = ''
    collect: list[dict] = field(default_factory=list)

    def line(self) -> str:
        return (f"status={self.status} films={self.films} | названия: к_работе={self.titles_todo} готово={self.titles_done} "
                f"отказ={self.titles_failed} склеек={self.merges} вето={self.merge_vetoes} | новых_жанров={self.genre_terms_new} | "
                f"описания: к_работе={self.desc_todo} переведено={self.translated} русских={self.copied_ru} "
                f"отказ={self.trans_failed} устарело={self.stale}")


def needs_titles(m: dict, st: CinemaAISettings) -> bool:
    return m['ai_version'] != T.TITLES_VERSION and m['ai_titles_fails'] < st.max_fails


def needs_translation(m: dict, st: CinemaAISettings) -> bool:
    if not m['description_src_len'] or m['ai_desc_fails'] >= st.max_fails:
        return False
    if m['description_ru'] is None:
        return True
    done = m['translated_src_len']
    return bool(done) and m['description_src_len'] >= done * st.retranslate_ratio


def plan(movies: list[dict], st: CinemaAISettings) -> tuple[list[dict], list[dict]]:
    """(фильмы для этапа A, фильмы для этапа B) — самые «сеансные» первыми, не больше max_films_per_run."""
    a = [m for m in movies if needs_titles(m, st)][:st.max_films_per_run]
    b = [m for m in movies if needs_translation(m, st)][:st.max_films_per_run]
    return a, b


async def run_pass(pool, client, st: CinemaAISettings, *, apply: bool = True) -> PassResult:
    res = PassResult()
    movies = await store.load_active(pool)
    res.films = len(movies)
    todo_a, todo_b = plan(movies, st)
    res.titles_todo = len(todo_a)
    if not client.configured:
        res.status, res.error = 'skipped', 'нет OPENCODE_TRANSLATE_API_KEY или моделей'
        logger.warning(f"CINEMA_AI_SKIP | {res.error}")
        return res

    client.begin_run()
    stop = asyncio.Event()
    sem = asyncio.Semaphore(st.concurrency)
    genre_map = await store.load_genre_map(pool)

    async def guarded(coro_fn):
        async with sem:
            if stop.is_set():
                return None
            try:
                return await coro_fn()
            except AIUnavailable as e:
                stop.set()
                res.error = res.error or str(e)
                logger.warning(f"CINEMA_AI_STOP | {e}")
                return None

    # ---------------------------------------------------------------- этап A: названия и склейка
    variants: dict[int, list[str]] = {}
    if todo_a:
        variants = await store.load_variants(pool, [m['id'] for m in movies])
        by_id = {m['id']: m for m in movies}
        junk = T.junk_titles(variants)
        batches = [todo_a[i:i + st.titles_batch] for i in range(0, len(todo_a), st.titles_batch)]
        ok_all: dict[int, T.TitleResult] = {}
        failed: list[int] = []

        async def run_batch(batch: list[dict]):
            ids = {m['id'] for m in batch}
            ctx_ids: list[int] = []
            for m in batch:
                for c in T.find_candidates(m['id'], variants, st.candidate_similarity, st.max_candidates, junk):
                    if c not in ids and c not in ctx_ids:
                        ctx_ids.append(c)
            row = lambda i: (i, variants[i], by_id[i]['description_src'] or '')           # noqa: E731
            user = T.build_user_prompt([row(m['id']) for m in batch], [row(i) for i in ctx_ids])
            try:
                out = await client.complete(T.SYSTEM_PROMPT, user)
                ok, bad = T.parse_titles(out, ids, ids | set(ctx_ids))
            except T.BadTitles as e:
                client.report_bad_output()
                logger.warning(f"CINEMA_AI_TITLES_BAD | {e}")
                failed.extend(ids)
                return None
            if ok:
                client.report_ok()
            ok_all.update(ok)
            failed.extend(bad)
            for fid, why in bad.items():
                logger.warning(f"CINEMA_AI_TITLES_BAD | id={fid} | {why}")
            return True

        await asyncio.gather(*[guarded(lambda b=b: run_batch(b)) for b in batches])

        pointers = {i: r.same_as for i, r in ok_all.items() if r.same_as}
        losers = T.resolve_merges(pointers)
        vetoed: set[int] = set()
        for loser, winner in list(losers.items()):
            if not T.numbers_compatible(variants.get(loser, []), variants.get(winner, [])):
                vetoed.add(loser)
                del losers[loser]
                res.merge_vetoes += 1
                logger.warning(f"CINEMA_AI_MERGE_VETO | {variants.get(loser)} !~ {variants.get(winner)} (разные номера частей)")
        model = client.current_model
        for fid, r in ok_all.items():
            winner = losers.get(fid)
            res.collect.append({'kind': 'title', 'id': fid, 'variants': ' | '.join(variants.get(fid, [])), 'en': r.en,
                                'ru': r.ru or '', 'same_as': r.same_as or '', 'merge_into': winner or '',
                                'veto': 'да' if fid in vetoed else '',
                                'sessions': by_id[fid]['sessions']})
            if winner is None and apply:
                await store.save_titles(pool, fid, r.en, r.ru, T.TITLES_VERSION, model)
        if apply:
            for loser, winner in losers.items():
                if await store.merge_movies(pool, loser, winner):
                    res.merges += 1
        else:
            res.merges = len(losers)
        res.titles_done = len(ok_all)
        res.titles_failed = len(failed)
        if apply:
            await store.bump_fail(pool, failed, 'titles')

    # ---------------------------------------------------------------- жанры
    ids_all = [m['id'] for m in movies]
    terms_by_film = await store.load_genre_terms(pool, ids_all) if ids_all else {}
    unknown = sorted({t for rows in terms_by_film.values() for t in store.terms_of(rows)} - set(genre_map))
    res.genre_terms_new = len(unknown)
    for i in range(0, len(unknown), GENRE_CHUNK):
        chunk = unknown[i:i + GENRE_CHUNK]

        async def run_genres(chunk=chunk):
            out = await client.complete(G.SYSTEM_PROMPT, G.build_user_prompt(chunk))
            try:
                parsed = G.parse_genres(out, chunk)
            except ValueError as e:
                client.report_bad_output()
                logger.warning(f"CINEMA_AI_GENRES_BAD | {e}")
                return None
            client.report_ok()
            return parsed

        parsed = await guarded(run_genres)
        if parsed:
            genre_map.update(parsed)
            for term, g in parsed.items():
                res.collect.append({'kind': 'genre', 'term': term, 'genre_ru': g or ''})
            if apply:
                await store.save_genre_map(pool, parsed)
    if apply and ids_all:
        genres_by_film = {fid: G.film_genres(terms_by_film.get(fid, []), genre_map, store.SOURCE_PRIORITY) for fid in ids_all}
        await store.refresh_status_and_genres(pool, genres_by_film, st.max_fails)

    # ---------------------------------------------------------------- этап B: перевод описаний
    if apply:                                   # после склейки описания и набор фильмов могли измениться
        movies = await store.load_active(pool)
        _, todo_b = plan(movies, st)
    res.desc_todo = len(todo_b)

    async def translate(m: dict):
        text = m['description_src'] or ''
        src_len = m['description_src_len']
        if is_russian(text):
            if apply and await store.save_description(pool, m['id'], text, src_len, None):
                res.copied_ru += 1
            res.collect.append({'kind': 'desc', 'id': m['id'], 'title': m['title_serbian'], 'src_len': src_len,
                                'ru_len': len(text), 'text': text[:400], 'note': 'уже по-русски'})
            return
        sent = cut_input(text, st.input_chars)
        try:
            out = await client.complete(TRANSLATE_PROMPT, sent)
            validate(out, sent)
        except BadTranslation as e:
            client.report_bad_output()
            res.trans_failed += 1
            if apply:
                await store.bump_fail(pool, [m['id']], 'desc')
            logger.warning(f"CINEMA_AI_DESC_BAD | id={m['id']} | {e}")
            return
        client.report_ok()
        saved = True
        if apply:
            saved = await store.save_description(pool, m['id'], out.strip(), src_len, client.current_model)
        if saved:
            res.translated += 1
        else:
            res.stale += 1
        res.collect.append({'kind': 'desc', 'id': m['id'], 'title': m['title_serbian'], 'src_len': src_len,
                            'ru_len': len(out.strip()), 'text': out.strip(), 'note': ''})

    await asyncio.gather(*[guarded(lambda m=m: translate(m)) for m in todo_b])

    if apply and ids_all:                          # статусы после перевода
        movies = await store.load_active(pool)
        await store.refresh_status_and_genres(
            pool, {m['id']: G.film_genres(terms_by_film.get(m['id'], []), genre_map, store.SOURCE_PRIORITY) for m in movies},
            st.max_fails)
    if stop.is_set():
        res.status = 'incomplete'
    logger.info(f"CINEMA_AI_DONE | {res.line()} | {client.stats.line()}")
    return res
