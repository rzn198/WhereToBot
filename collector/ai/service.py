"""AI-парсинг дат и категорий messages: режимы, откат, статистика.

Режимы независимы:
  parsing.date_method:     regex | ai | shadow
  parsing.category_method: dict  | ai | shadow
  * regex/dict — прежняя логика, AI не вызывается (по умолчанию).
  * shadow     — AI считается и сравнивается (AI_DATE_DISAGREE / AI_CAT_DISAGREE),
                 в БД идёт прежняя логика.
  * ai         — в БД идёт ответ AI; при любом сбое — прежняя логика.

Откаты на прежнюю логику в режиме ai (флаги parsing.*):
  ai_null_date_fallback        (true)  AI «даты нет», regex нашёл → остаётся дата regex
  ai_empty_categories_fallback (true)  AI «категорий нет», словарь нашёл → остаются категории словаря
  ai_trust_non_event           (false) AI «ни даты, ни категорий» = пост не событие: оба отката выключаются
                                       для этого поста (дата и категории пустые). Если категории у AI есть,
                                       а даты нет — это событие без даты, откат на regex работает как обычно.
Случаи «AI считает не событием, а прежняя логика что-то нашла» пишутся в лог (AI_NON_EVENT) и считаются
во всех режимах, в том числе при выключенном флаге — чтобы оценить его в shadow.

Не переопределяется AI: дата у каналов с кастомным парсером и категории, назначенные
правилами `categorize_forced` (кастомный парсер, fixed_channels, ключевые слова канала).

Сбор сообщений не должен падать из-за AI: любая ошибка → regex-дата и словарные категории.
"""
from __future__ import annotations

from datetime import date

from loguru import logger

from collector.ai import extractor
from collector.categorizer_v2 import categorize_forced, category_names
from shared.ai import AIClient, AIUnavailable, client_from_config

DATE_MODES = ("regex", "ai", "shadow")
CAT_MODES = ("dict", "ai", "shadow")


def _mode(value: str, allowed: tuple, default: str, key: str) -> str:
    if value in allowed:
        return value
    logger.warning(f"AI_PARSING_BAD_MODE | {key}={value!r} → {default} (допустимо: {', '.join(allowed)})")
    return default


class AIParsingService:

    def __init__(self, cfg: dict | None = None, client: AIClient | None = None,
                 custom_channels: set[str] | None = None):
        self._cfg = cfg
        self._client = client
        self._custom = custom_channels
        self._ready = False
        self._cap_logged = False
        self._down_logged = False
        self._calls = 0
        self.counters = _fresh_counters()

    # ------------------------------------------------------------------ настройка

    def _init(self) -> None:
        if self._ready:
            return
        if self._cfg is None:
            from shared.config import config
            self._cfg = config.get('parsing') or {}
            if self._custom is None:
                self._custom = {e['name'].lstrip('@') for e in (config.get('channels') or {}).get('custom', [])}
        cfg = self._cfg
        self.date_mode = _mode(str(cfg.get('date_method', 'regex')).lower(), DATE_MODES, 'regex', 'date_method')
        self.cat_mode = _mode(str(cfg.get('category_method', 'dict')).lower(), CAT_MODES, 'dict', 'category_method')
        self.null_date_fallback = bool(cfg.get('ai_null_date_fallback', True))
        self.empty_cats_fallback = bool(cfg.get('ai_empty_categories_fallback', True))
        self.trust_non_event = bool(cfg.get('ai_trust_non_event', False))
        self.max_chars = int(cfg.get('max_chars', 2000))
        self.max_calls = int(cfg.get('max_calls_per_run', 1000))
        self.concurrency = max(1, int(cfg.get('concurrency', 4)))
        self._custom = self._custom or set()
        if self._client is None and (self.date_mode != 'regex' or self.cat_mode != 'dict'):
            self._client = client_from_config('parsing', cfg)
        self._ready = True
        if self.enabled:
            logger.info(
                f"AI_PARSING_INIT | date={self.date_mode} | categories={self.cat_mode} | "
                f"models={self._client.models if self._client else []} | "
                f"key={'set' if self._client and self._client.api_key else 'MISSING'}")

    @property
    def enabled(self) -> bool:
        self._init()
        return self.date_mode != 'regex' or self.cat_mode != 'dict'

    def _is_custom_channel(self, channel: str) -> bool:
        return channel.lstrip('@') in self._custom

    def decides_date(self, channel: str) -> bool:
        """AI определяет дату сохраняемого поста (режим ai, не кастомный канал)."""
        self._init()
        return self.date_mode == 'ai' and not self._is_custom_channel(channel)

    def needs_ai(self, text: str, channel: str, position: int = 0) -> bool:
        """Нужен ли вообще запрос к AI для этого поста."""
        self._init()
        want_date = self.date_mode != 'regex' and not self._is_custom_channel(channel)
        want_cat = self.cat_mode != 'dict' and categorize_forced(text, channel, position) is None
        return want_date or want_cat

    # ------------------------------------------------------------------ прогон

    def begin_run(self) -> None:
        self._init()
        self._calls = 0
        self._cap_logged = False
        self._down_logged = False
        self.counters = _fresh_counters()
        if self._client is not None:
            self._client.begin_run()

    async def end_run(self) -> None:
        self._init()
        if not self.enabled:
            return
        c = self.counters
        logger.info(
            f"AI_PARSING_RUN | resolved={c['resolved']} fallback={c['fallback']} "
            f"date_disagree={c['date_disagree']} cat_disagree={c['cat_disagree']} non_event={c['non_event']} "
            f"ai_used_date={c['ai_date']} ai_used_cats={c['ai_cats']} | "
            + (self._client.stats.line() if self._client else "no client"))
        if self._client is not None:
            await self._client.aclose()

    # ------------------------------------------------------------------ основное

    async def resolve(self, *, text: str, channel: str, position: int, published_at: date,
                      regex_date: date | None, dict_types: list[str]) -> tuple[date | None, list[str]]:
        """(дата, категории) для сохранения. Никогда не бросает исключений."""
        d, t, _ = await self.resolve_full(text=text, channel=channel, position=position,
                                          published_at=published_at, regex_date=regex_date, dict_types=dict_types)
        return d, t

    async def resolve_full(self, *, text: str, channel: str, position: int, published_at: date,
                           regex_date: date | None, dict_types: list[str]
                           ) -> tuple[date | None, list[str], bool]:
        """(дата, категории, ответил_ли_AI). Никогда не бросает исключений.
        Третье значение False — AI не вызывался или не ответил (сбой, лимит, нет ключа): пост не считается оценённым."""
        try:
            self._init()
            return await self._resolve(text, channel, position, published_at, regex_date, dict_types)
        except Exception as e:
            self.counters['fallback'] += 1
            logger.error(f"AI_PARSING_ERROR | channel={channel} | {type(e).__name__}: {e} — прежняя логика")
            return regex_date, dict_types, False

    async def _resolve(self, text, channel, position, published_at, regex_date, dict_types):
        forced = categorize_forced(text, channel, position) is not None
        want_date = self.date_mode != 'regex' and not self._is_custom_channel(channel)
        want_cat = self.cat_mode != 'dict' and not forced
        if not (want_date or want_cat):
            return regex_date, dict_types, False

        result = await self._ask(text, published_at)
        if result is None:
            self.counters['fallback'] += 1
            return regex_date, dict_types, False
        self.counters['resolved'] += 1

        out_date, out_types = regex_date, dict_types
        label = f"channel={channel} | pos={position} | {text[:80]!r}"

        # AI: «не событие» (ни даты, ни категорий). Посты с категориями из правил не трогаем.
        non_event = result.date is None and not result.categories and not forced
        if non_event and (regex_date is not None or dict_types):
            self.counters['non_event'] += 1
            logger.info(f"AI_NON_EVENT | trust={self.trust_non_event} | regex={regex_date} | dict={dict_types} | {label}")
        trust = non_event and self.trust_non_event

        if want_date:
            if result.date != regex_date:
                self.counters['date_disagree'] += 1
                logger.info(f"AI_DATE_DISAGREE | mode={self.date_mode} | regex={regex_date} | "
                            f"ai={result.date} | {label}")
            if self.date_mode == 'ai':
                if result.date is None and regex_date is not None and self.null_date_fallback and not trust:
                    pass                                   # AI «даты нет», regex нашёл — оставляем regex
                else:
                    out_date = result.date
                    self.counters['ai_date'] += 1

        if want_cat:
            if set(result.categories) != set(dict_types):
                self.counters['cat_disagree'] += 1
                logger.info(f"AI_CAT_DISAGREE | mode={self.cat_mode} | dict={dict_types} | "
                            f"ai={result.categories} | {label}")
            if self.cat_mode == 'ai':
                if not result.categories and dict_types and self.empty_cats_fallback and not trust:
                    pass                                   # AI «ничего», словарь нашёл — оставляем словарь
                else:
                    out_types = result.categories
                    self.counters['ai_cats'] += 1

        return out_date, out_types, True

    async def _ask(self, text: str, published_at: date):
        client = self._client
        if client is None or not client.available:
            if not self._down_logged:
                self._down_logged = True
                logger.warning("AI_PARSING_UNAVAILABLE | нет ключа/моделей или модели исчерпаны — прежняя логика")
            return None
        if self._calls >= self.max_calls:
            if not self._cap_logged:
                self._cap_logged = True
                logger.warning(f"AI_PARSING_CALL_CAP | max_calls_per_run={self.max_calls} — остальное прежней логикой")
            return None
        self._calls += 1
        try:
            return await extractor.extract(client, text, published_at, category_names(), self.max_chars)
        except AIUnavailable as e:
            if not self._down_logged:
                self._down_logged = True
                logger.warning(f"AI_PARSING_UNAVAILABLE | {e} — прежняя логика")
            return None
        except extractor.BadAIOutput:
            return None


def _fresh_counters() -> dict:
    return dict(resolved=0, fallback=0, date_disagree=0, cat_disagree=0, non_event=0, ai_date=0, ai_cats=0)


ai_service = AIParsingService()
