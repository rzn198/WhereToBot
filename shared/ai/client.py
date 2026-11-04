"""Общий клиент OpenCode (OpenAI-совместимый /chat/completions) для AI-процессов.

Тот же подход, что в переводчике (`event_collector/translator.py`):
  * список моделей и «залипающий» указатель: ошибка модели сдвигает указатель вперёд
    и он остаётся там до конца прогона;
  * сеть и 5xx ретраятся на месте, 429/400/прочее сдвигают модель;
  * 401/403 — ключ сломан, AI выключается на весь прогон;
  * заголовки OpenCode Go: свой User-Agent и стабильный на прогон x-opencode-session.

Отличия: у каждого процесса (parsing, dedup) свой клиент — свой ключ, свои модели и своё
имя в User-Agent и логах, чтобы в панели OpenCode и в логах было видно, кто тратит запросы.
Проверку смысла ответа делает потребитель; он сообщает клиенту `report_ok()` /
`report_bad_output()`: несколько невалидных ответов подряд сдвигают модель.
"""
from __future__ import annotations

import asyncio
import uuid
from collections import Counter
from dataclasses import dataclass, field

import httpx
from loguru import logger

DEFAULT_BASE_URL = "https://opencode.ai/zen/go/v1"
TEMP_RETRIES = 2
TEMP_RETRY_DELAY = 1.0
BAD_OUTPUT_SHIFT = 5


class AIUnavailable(Exception):
    """AI сейчас недоступен (нет ключа/моделей, модели исчерпаны, ключ отвергнут)."""


class AIAuthError(AIUnavailable):
    """401/403: ключ не принят, AI выключен до следующего begin_run()."""


class _ModelError(Exception):
    """Ошибка текущей модели: сдвинуть указатель."""


@dataclass
class AIStats:
    calls: int = 0
    ok: int = 0
    model_errors: int = 0
    bad_outputs: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    by_model: Counter = field(default_factory=Counter)

    def line(self) -> str:
        models = ",".join(f"{m}:{n}" for m, n in self.by_model.items()) or "-"
        return (f"calls={self.calls} ok={self.ok} model_errors={self.model_errors} "
                f"bad_outputs={self.bad_outputs} tokens_in={self.prompt_tokens} "
                f"tokens_out={self.completion_tokens} models={models}")


class AIClient:

    def __init__(self, name: str, api_key: str, models: list[str], *,
                 base_url: str = DEFAULT_BASE_URL, timeout: float = 60,
                 transport: httpx.AsyncBaseTransport | None = None,
                 retry_delay: float = TEMP_RETRY_DELAY,
                 bad_output_shift: int = BAD_OUTPUT_SHIFT,
                 temperature: float | None = None):
        self.name = name
        self.api_key = (api_key or "").strip()
        self.models = [m for m in (models or []) if m]
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout
        self.user_agent = f"wheretobot-{name}/1.0"
        self._transport = transport
        self._retry_delay = retry_delay
        self._bad_shift = bad_output_shift
        self.temperature = temperature
        self._http: httpx.AsyncClient | None = None
        self.begin_run()

    # ------------------------------------------------------------------ состояние прогона

    def begin_run(self) -> None:
        self._idx = 0
        self._dead = False
        self._bad_in_row = 0
        self._session_id = str(uuid.uuid4())
        self.stats = AIStats()

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.models)

    @property
    def available(self) -> bool:
        return self.configured and not self._dead and self._idx < len(self.models)

    @property
    def current_model(self) -> str | None:
        return self.models[self._idx] if self._idx < len(self.models) else None

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    # ------------------------------------------------------------------ запрос

    async def complete(self, system: str, user: str) -> str:
        """Ответ модели. Сдвигает модели при сбоях; AIUnavailable, если дальше некуда."""
        if not self.configured:
            raise AIUnavailable(f"{self.name}: не заданы ключ или модели")
        while True:
            if self._dead:
                raise AIUnavailable(f"{self.name}: ключ отвергнут")
            if self._idx >= len(self.models):
                raise AIUnavailable(f"{self.name}: все модели исчерпаны")
            model = self.models[self._idx]
            self.stats.calls += 1
            try:
                text = await self._call(model, system, user)
            except _ModelError as e:
                self.stats.model_errors += 1
                logger.warning(f"AI_{self.name.upper()}_MODEL_SWITCH | model={model} | {e}")
                self._shift()
                continue
            self.stats.ok += 1
            self.stats.by_model[model] += 1
            return text

    def report_ok(self) -> None:
        self._bad_in_row = 0

    def report_bad_output(self) -> None:
        """Ответ получен, но не прошёл проверку потребителя."""
        self.stats.bad_outputs += 1
        self._bad_in_row += 1
        if self._bad_in_row >= self._bad_shift:
            logger.warning(
                f"AI_{self.name.upper()}_MODEL_SWITCH | model={self.current_model} | "
                f"{self._bad_in_row} невалидных ответов подряд")
            self._shift()

    def _shift(self) -> None:
        self._idx += 1
        self._bad_in_row = 0

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(transport=self._transport)
        return self._http

    async def _call(self, model: str, system: str, user: str) -> str:
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": self.user_agent,
            "x-opencode-session": self._session_id,
        }
        payload = {
            "model": model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
        }
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        attempt = 0
        while True:
            try:
                r = await self._client().post(url, json=payload, headers=headers, timeout=self.timeout)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                attempt += 1
                if attempt <= TEMP_RETRIES:
                    logger.warning(f"AI_{self.name.upper()}_NET_RETRY | model={model} | try={attempt} | {e}")
                    await asyncio.sleep(self._retry_delay)
                    continue
                raise _ModelError(f"network: {e}")

            code = r.status_code
            if code == 200:
                return self._extract(r)
            if code in (401, 403):
                self._dead = True
                logger.error(f"AI_{self.name.upper()}_AUTH_STOP | {code} — проверь ключ процесса '{self.name}'")
                raise AIAuthError(f"{self.name}: auth {code}")
            if code == 429:
                raise _ModelError("rate/quota 429")
            if code == 400:
                logger.warning(f"AI_{self.name.upper()}_400 | model={model} | body={r.text[:200]}")
                raise _ModelError("bad request 400")
            if 500 <= code < 600:
                attempt += 1
                if attempt <= TEMP_RETRIES:
                    logger.warning(f"AI_{self.name.upper()}_5XX_RETRY | model={model} | {code} | try={attempt}")
                    await asyncio.sleep(self._retry_delay)
                    continue
                raise _ModelError(f"server {code}")
            raise _ModelError(f"http {code}")

    def _extract(self, r: httpx.Response) -> str:
        try:
            data = r.json()
            text = (data["choices"][0]["message"]["content"] or "").strip()
        except (KeyError, IndexError, ValueError, TypeError) as e:
            raise _ModelError(f"parse response: {e}")
        if not text:
            raise _ModelError("empty result")
        usage = data.get("usage") if isinstance(data, dict) else None
        if isinstance(usage, dict):
            self.stats.prompt_tokens += int(usage.get("prompt_tokens") or 0)
            self.stats.completion_tokens += int(usage.get("completion_tokens") or 0)
        return text


def client_from_config(name: str, cfg: dict, **kwargs) -> AIClient:
    """Клиент процесса из секции конфига: api_key, models, base_url, timeout."""
    return AIClient(
        name,
        cfg.get("api_key", ""),
        cfg.get("models") or [],
        base_url=cfg.get("base_url") or DEFAULT_BASE_URL,
        timeout=float(cfg.get("timeout") or 60),
        temperature=cfg.get("temperature"),
        **kwargs,
    )
