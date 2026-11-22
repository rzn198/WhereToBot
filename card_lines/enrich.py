"""Обогащение фактами из интернета для записей, у которых описания нет или оно слишком короткое.

Источники (открытые API, без ключей): MusicBrainz (артисты) и Wikipedia (ru, en, sr). Модели из цепочки в интернет сами не ходят:
поиск делает этот код, а найденный текст отдаётся модели отдельным блоком данных (хук обязан цитировать его).

Строгость выбора (лучше без хука, чем не тот артист):
  * MusicBrainz: принимается только артист с ТОЧНЫМ именем (без регистра и диакритики) и баллом ≥ 90; если таких несколько,
    выбираем по подсказке из описания события (страна, жанр, пояснение), иначе «неоднозначно» и фактов нет;
  * Wikipedia: страница с тем же названием (или переадресация с него, или «Название (…)»), не страница-значение, и
    описание/вводный абзац должны говорить, что это музыкант или группа;
  * берутся только устойчивые факты: тип, страна/город, жанры, годы активности, вводный абзац.
Результат поиска (и «не найдено») кэшируется в card_enrichment. Сетевые сбои не кэшируются: EnrichUnavailable.
Лимит MusicBrainz: не чаще 1 запроса в секунду; у запросов обязательный User-Agent с контактом.
"""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from urllib.parse import quote

import httpx
from loguru import logger

from card_lines.norm import fold
from card_lines.settings import EnrichSettings

MB_URL = 'https://musicbrainz.org/ws/2/artist/'
WP_URL = 'https://{lang}.wikipedia.org/w/api.php'
MAX_FACTS_CHARS = 900
GENERIC_TITLES = {'koncert', 'koncerti', 'concert', 'concerts', 'концерт', 'концерты', 'музыка', 'live', 'live music', 'dj set'}

_MUSIC = re.compile(
    r'групп|музык|певец|певиц|исполнител|композитор|рэпер|диджей|гитарист|пианист|скрипач|дуэт|оркестр|ансамбл|хор\b|'
    r'band|singer|musician|rapper|composer|guitarist|pianist|violin|duo\b|orchestra|choir|\bdj\b|songwriter|'
    r'певач|музичар|група|бенд|оркестар|kompozitor|pevac|pevacic|grupa')
_SPLIT = re.compile(r'\s*(?:,|/|&|\+|;)\s*|\s+(?:x|feat\.?|ft\.?|and|и|vs\.?)\s+', re.I)
_PARENS = re.compile(r'\([^)]*\)|\[[^\]]*\]')


class EnrichUnavailable(Exception):
    """Сеть или сервис недоступны: результат не кэшируется, хук повторим позже."""


@dataclass(frozen=True)
class Lookup:
    status: str                 # found | not_found | ambiguous
    facts: str = ''
    url: str = ''


@dataclass(frozen=True)
class Facts:
    text: str
    url: str
    source: str                 # например 'MusicBrainz + Wikipedia (ru)'


def artist_names(title: str) -> list[str]:
    """Имена исполнителей из названия: «ACO PEJOVIĆ & EDITA ARADINOVIĆ» -> два имени (берём не больше двух)."""
    t = _PARENS.sub(' ', title or '')
    names = [re.sub(r'\s+', ' ', n).strip(' -–—:.') for n in _SPLIT.split(t)]
    return [n for n in names if len(n) >= 2][:2]


def _clip(text: str, limit: int) -> str:
    text = re.sub(r'\s+', ' ', text).strip()
    return text if len(text) <= limit else text[:limit].rsplit(' ', 1)[0] + '…'


# ------------------------------------------------------------------ кэш

class MemoryCache:
    """Кэш в памяти процесса (скрипт сравнения моделей: в БД ничего не пишет)."""

    def __init__(self):
        self.data: dict[str, Lookup] = {}

    async def get(self, key: str) -> Lookup | None:
        return self.data.get(key)

    async def put(self, key: str, source: str, query: str, lookup: Lookup, ttl_days: int) -> None:
        self.data[key] = lookup


class DbCache:
    def __init__(self, pool):
        self.pool = pool

    async def get(self, key: str) -> Lookup | None:
        from db import card_lines as store
        return await store.get_enrichment(self.pool, key)

    async def put(self, key: str, source: str, query: str, lookup: Lookup, ttl_days: int) -> None:
        from db import card_lines as store
        await store.put_enrichment(self.pool, key, source, query, lookup, ttl_days)


# ------------------------------------------------------------------ клиент

class Enricher:
    def __init__(self, st: EnrichSettings, cache, transport=None, sleep=asyncio.sleep, clock=time.monotonic):
        self.st, self.cache = st, cache
        self._transport, self._sleep, self._clock = transport, sleep, clock
        self._client: httpx.AsyncClient | None = None
        self._locks: dict[str, asyncio.Lock] = {}
        self._last: dict[str, float] = {}
        self.stats = {'lookups': 0, 'cache_hits': 0, 'requests': 0}

    @property
    def user_agent(self) -> str:
        return f"WhereToBotBG/1.0 ( {self.st.contact or 'no-contact-configured'} )"

    def _lock(self, name: str) -> asyncio.Lock:
        return self._locks.setdefault(name, asyncio.Lock())

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _get(self, url: str, params: dict, host: str) -> dict:
        """GET JSON с лимитом частоты по хосту (MusicBrainz ≥ mb_min_interval секунд между запросами).
        На 429/503 (MusicBrainz так отвечает при перегрузке) повторяем с паузой (Retry-After или 3, 8 с); потом EnrichUnavailable."""
        interval = self.st.mb_min_interval if host == 'mb' else 0.1
        last = ''
        for attempt, pause in enumerate((0, 3, 8)):
            if pause:
                await self._sleep(min(pause, 30))
            async with self._lock('http:' + host):
                wait = self._last.get(host, -1e9) + interval - self._clock()
                if wait > 0:
                    await self._sleep(wait)
                if self._client is None:
                    self._client = httpx.AsyncClient(timeout=self.st.timeout, transport=self._transport,
                                                     headers={'User-Agent': self.user_agent, 'Accept': 'application/json'})
                self._last[host] = self._clock()
                self.stats['requests'] += 1
                try:
                    r = await self._client.get(url, params=params)
                except httpx.HTTPError as e:
                    last = f"{host}: {type(e).__name__}"
                    continue
            if r.status_code == 429 or r.status_code >= 500:
                last = f"{host}: HTTP {r.status_code}"
                try:
                    pause_hdr = float(r.headers.get('retry-after', ''))
                except ValueError:
                    pause_hdr = 0
                if pause_hdr and attempt < 2:
                    await self._sleep(min(pause_hdr, 30))
                continue
            if r.status_code >= 400:              # плохой запрос (например, странное название): это «не найдено», а не сбой сети
                logger.info(f"ENRICH_HTTP_{r.status_code} | {host} | {params.get('query') or params.get('titles')}")
                return {}
            try:
                data = r.json()
            except ValueError as e:
                raise EnrichUnavailable(f"{host}: не JSON") from e
            return data if isinstance(data, dict) else {}
        raise EnrichUnavailable(last or f"{host}: недоступен")

    # --- MusicBrainz

    @staticmethod
    def _pick_by_hint(cands: list[dict], hint: str) -> dict | None:
        hf = fold(hint or '')
        best: list[tuple[int, dict]] = []
        for a in cands:
            words = [fold(a.get('disambiguation') or '')]
            words += [fold(t.get('name', '')) for t in (a.get('tags') or [])]
            words += [fold((a.get('area') or {}).get('name', '')), fold(a.get('country') or '')]
            score = sum(1 for w in words if len(w) >= 4 and w in hf)
            best.append((score, a))
        best.sort(key=lambda x: -x[0])
        if best and best[0][0] > 0 and (len(best) == 1 or best[0][0] > best[1][0]):
            return best[0][1]
        return None

    @staticmethod
    def _mb_facts(a: dict) -> str:
        bits = [f"MusicBrainz: {a.get('name')}"]
        if a.get('type'):
            bits.append(f"type: {a['type']}")
        origin = (a.get('area') or {}).get('name') or a.get('country')
        if origin:
            bits.append(f"origin: {origin}" + (f" ({a['country']})" if a.get('country') and origin != a['country'] else ''))
        begin = ((a.get('life-span') or {}).get('begin') or '')[:4]
        if begin:
            bits.append(f"active since: {begin}")
        tags = sorted(a.get('tags') or [], key=lambda t: -int(t.get('count') or 0))[:5]
        if tags:
            bits.append("tags: " + ", ".join(t['name'] for t in tags if t.get('name')))
        return '; '.join(bits)

    async def _musicbrainz(self, name: str, hint: str) -> Lookup:
        data = await self._get(MB_URL, {'query': 'artist:"' + name.replace('\\', ' ').replace('"', ' ') + '"', 'fmt': 'json', 'limit': '5'}, 'mb')
        exact = [a for a in data.get('artists') or []
                 if isinstance(a, dict) and fold(a.get('name') or '') == fold(name) and int(a.get('score') or 0) >= 90]
        if not exact:
            return Lookup('not_found')
        pick = exact[0] if len(exact) == 1 else self._pick_by_hint(exact, hint)
        if pick is None:
            return Lookup('ambiguous')
        return Lookup('found', self._mb_facts(pick), f"https://musicbrainz.org/artist/{pick.get('id')}")

    # --- Wikipedia

    async def _wikipedia(self, lang: str, name: str) -> Lookup:
        params = {'action': 'query', 'format': 'json', 'formatversion': '2', 'redirects': '1', 'titles': name,
                  'prop': 'extracts|pageprops|description', 'exintro': '1', 'explaintext': '1', 'exsentences': '4',
                  'ppprop': 'disambiguation'}
        data = await self._get(WP_URL.format(lang=lang), params, 'wp')
        q = data.get('query') or {}
        pages = q.get('pages') or []
        if not pages or not isinstance(pages[0], dict) or pages[0].get('missing'):
            return Lookup('not_found')
        pg = pages[0]
        if 'disambiguation' in (pg.get('pageprops') or {}):
            return Lookup('ambiguous')
        title = str(pg.get('title') or '')
        fname, ftitle = fold(name), fold(title)
        from_exact = any(fold(str(r.get('from') or '')) == fname for r in q.get('redirects') or [] if isinstance(r, dict))
        if not (ftitle == fname or ftitle.startswith(fname + ' (') or from_exact):
            return Lookup('not_found')
        extract = str(pg.get('extract') or '').strip()
        desc = str(pg.get('description') or '').strip()
        if not extract or not _MUSIC.search(fold(f"{desc} {extract[:300]}")):
            return Lookup('not_found')                  # не музыкант: однофамилец или другая тема
        text = f"Wikipedia ({lang}): {title}" + (f" — {desc}" if desc else '') + "\n" + _clip(extract, 600)
        return Lookup('found', text, f"https://{lang}.wikipedia.org/wiki/{quote(title.replace(' ', '_'))}")

    # --- общий вход

    async def _cached(self, key: str, source: str, query: str, fetch) -> Lookup:
        async with self._lock('key:' + key):
            hit = await self.cache.get(key)
            if hit is not None:
                self.stats['cache_hits'] += 1
                return hit
            self.stats['lookups'] += 1
            res = await fetch()
            ttl = {'found': self.st.ttl_found_days, 'ambiguous': self.st.ttl_ambiguous_days}.get(res.status, self.st.ttl_missing_days)
            await self.cache.put(key, source, query, res, ttl)
            return res

    async def facts_for(self, kind: str, title: str, hint: str = '') -> Facts | None:
        """Факты об исполнителе по названию записи. None — не найдено или неоднозначно; EnrichUnavailable — сбой сети
        (и ничего найти не удалось)."""
        if kind != 'artist':
            return None
        names = artist_names(title)
        if not names or len(names[0].split()) > 6 or fold(names[0]) in GENERIC_TITLES:
            return None
        name = names[0]
        key_name = fold(name)
        found: list[Lookup] = []
        labels: list[str] = []
        failed = False
        if 'musicbrainz' in self.st.sources:
            try:
                r = await self._cached(f"mb:{key_name}", 'musicbrainz', name, lambda: self._musicbrainz(name, hint))
                if r.status == 'found':
                    found.append(r)
                    labels.append('MusicBrainz')
            except EnrichUnavailable as e:
                failed = True
                logger.warning(f"ENRICH_UNAVAILABLE | {e}")
        if 'wikipedia' in self.st.sources:
            for lang in self.st.languages:
                try:
                    r = await self._cached(f"wp:{lang}:{key_name}", f'wikipedia:{lang}', name,
                                           lambda lang=lang: self._wikipedia(lang, name))
                except EnrichUnavailable as e:
                    failed = True
                    logger.warning(f"ENRICH_UNAVAILABLE | {e}")
                    continue
                if r.status == 'found':
                    found.append(r)
                    labels.append(f"Wikipedia ({lang})")
                    break
        if not found:
            if failed:
                raise EnrichUnavailable("источники недоступны")
            return None
        text = _clip("\n".join(f.facts for f in found), MAX_FACTS_CHARS)
        url = next((f.url for f in reversed(found) if f.url), '')
        return Facts(text, url, ' + '.join(labels))
