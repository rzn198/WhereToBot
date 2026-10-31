from __future__ import annotations

import asyncio
import re
import uuid
from time import sleep

import httpx
from loguru import logger

from db.tables import Tables
from shared.config import config

TICKETS_LINK_TEXT = "ссылка на билеты здесь"
ELLIPSIS = "..."

TEMP_RETRIES = 2
TEMP_RETRY_DELAY = 1.0

MAX_LEN_RATIO = 3
# ratio проверяем только для достаточно длинных результатов — на очень
# коротких (1-2 слова) даже нормальный перевод может дать высокий ratio
MIN_LEN_FOR_RATIO = 100

# User-Agent — свой, не generic (требование OpenCode Go)
USER_AGENT = "wheretobot-collector/1.0"

SYSTEM_PROMPT = (
    "Ты профессиональный переводчик. Переводишь текст на русский язык. "
    "Выводи ТОЛЬКО перевод — без пояснений, без префиксов, без кавычек. "
    "Если текст уже на русском — верни его без изменений. Сохраняй абзацы."
)


def escape_md(text: str) -> str:
    """Идентично боту (bot/handlers/contact.py): экранируем _ * ` [ ] ."""
    return re.sub(r'([_*`\[\]])', r'\\\1', text)


def _truncate_word_boundary(text: str, limit: int) -> str:
    """Обрезка до limit по границе слова + '...'. Если короче — как есть."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    # отступаем до последнего пробела, чтобы не резать слово
    sp = cut.rfind(" ")
    if sp > 0:
        cut = cut[:sp]
    return cut.rstrip() + ELLIPSIS


def _build_description_ru(translated: str, source_url: str, max_chars: int) -> str:
    """Обрезка -> escape текста -> добавить Markdown-ссылку (сырую)."""
    truncated = _truncate_word_boundary(translated.strip(), max_chars)
    escaped = escape_md(truncated)
    link = f"[{TICKETS_LINK_TEXT}]({source_url})"
    return f"{escaped}\n\n{link}"


class _AuthError(Exception):
    """401/403 — ключ сломан, стоп всего прогона."""


class _ModelError(Exception):
    """Ошибка текущей модели — нужно сдвинуть указатель (залипание)."""


async def _call_model(client: httpx.AsyncClient, base_url: str, api_key: str,
                      model: str, text: str, session_id: str) -> str:
    """
    Один запрос перевода к конкретной модели.
    Возвращает перевод или бросает:
      _AuthError  — 401/403 (стоп прогона)
      _ModelError — 429/400/5xx/сеть/плохой результат (сдвиг модели)
    Временные (сеть/5xx) ретраятся внутри TEMP_RETRIES раз.
    """
    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        # OpenCode Go требует эти заголовки, иначе MissingSessionID:
        "User-Agent": USER_AGENT,
        "x-opencode-session": session_id,
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
    }

    attempt = 0
    while True:
        try:
            r = await client.post(url, json=payload, headers=headers, timeout=60)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            # сеть — временная, ретраим
            attempt += 1
            if attempt <= TEMP_RETRIES:
                logger.warning(f"TRANSLATE_NET_RETRY | model={model} | try={attempt} | {e}")
                await asyncio.sleep(TEMP_RETRY_DELAY)
                continue
            raise _ModelError(f"network: {e}")

        code = r.status_code
        if code == 200:
            result = _extract_text(r)
            _validate(result, text)          # бросит _ModelError при плохом
            return result
        if code in (401, 403):
            raise _AuthError(f"auth {code}")
        if code == 429:
            raise _ModelError("rate/quota 429")
        if code == 400:
            # кривой запрос на этом событии — залогировать, сдвинуть модель
            logger.warning(f"TRANSLATE_400 | model={model} | body={r.text[:200]}")
            raise _ModelError("bad request 400")
        if 500 <= code < 600:
            attempt += 1
            if attempt <= TEMP_RETRIES:
                logger.warning(f"TRANSLATE_5XX_RETRY | model={model} | {code} | try={attempt}")
                await asyncio.sleep(TEMP_RETRY_DELAY)
                continue
            raise _ModelError(f"server {code}")
        # прочее — как ошибка модели
        raise _ModelError(f"http {code}")


def _extract_text(r: httpx.Response) -> str:
    """Достаём content из OpenAI-совместимого ответа."""
    try:
        data = r.json()
        return (data["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, ValueError, TypeError) as e:
        raise _ModelError(f"parse response: {e}")


def _validate(result: str, original: str) -> None:
    """Минимальная валидация: не пусто + не аномально длинный."""
    if not result or not result.strip():
        raise _ModelError("empty result")
    # ratio-проверка только для длинных результатов (короткие пропускаем —
    # там разброс велик, а вреда от короткого нет)
    if len(result) > MIN_LEN_FOR_RATIO and \
            len(result) > MAX_LEN_RATIO * max(len(original), 1):
        # логируем тексты — понять, ложное ли срабатывание (короткий оригинал
        # с нормальным, но длиннее порога переводом) или реально болтовня
        logger.warning(
            f"TRANSLATE_SUSPICIOUS | len {len(result)} vs {len(original)}\n"
            f"  ОРИГИНАЛ: {original[:200]!r}\n"
            f"  ПЕРЕВОД:  {result[:300]!r}"
        )
        raise _ModelError(f"suspicious length {len(result)} vs {len(original)}")


async def translate_new(pool) -> int:
    """
    Переводит новые события (description_ru IS NULL). Сохраняет обрезанный
    перевод + ссылку в description_ru. Возвращает число переведённых.
    """
    tg = config.get("opencode", {})
    api_key = tg.get("api_key")
    base_url = tg.get("base_url") or "https://opencode.ai/zen/go/v1"
    models = tg.get("models") or []
    max_chars = int(tg.get("max_chars") or 600)
    print('TRANSLATE_START')
    sleep(6000000)

    if not api_key or not models:
        logger.warning("TRANSLATE_SKIP | opencode api_key/models не заданы")
        return 0

    rows = await pool.fetch(f"""
        SELECT id, description, source_url
        FROM {Tables.EVENTS}
        WHERE description_ru IS NULL
          AND description IS NOT NULL
          AND description != ''
        ORDER BY id
    """)
    if not rows:
        logger.info("TRANSLATE | нет новых для перевода")
        return 0

    logger.info(f"TRANSLATE_START | новых={len(rows)} | моделей={len(models)}")
    idx = 0            # залипающий указатель текущей модели
    session_id = str(uuid.uuid4())   # стабильный на весь прогон (OpenCode Go)
    done = 0

    async with httpx.AsyncClient() as client:
        for row in rows:
            if idx >= len(models):
                logger.warning(
                    f"TRANSLATE_STOP | все модели исчерпаны | "
                    f"переведено={done} осталось={len(rows) - done}"
                )
                break

            text = row["description"]
            translated = None

            # пробуем модели начиная с текущего idx; ошибка сдвигает idx
            while idx < len(models):
                model = models[idx]
                try:
                    translated = await _call_model(
                        client, base_url, api_key, model, text, session_id
                    )
                    break
                except _AuthError as e:
                    logger.error(f"TRANSLATE_AUTH_STOP | {e} — проверь opencode api_key")
                    return done         # стоп всего прогона
                except _ModelError as e:
                    logger.warning(f"TRANSLATE_MODEL_SWITCH | model={model} | {e}")
                    idx += 1            # залипающий сдвиг вперёд

            if translated is None:
                # все модели кончились на этом событии — остальное в след. прогон
                continue

            description_ru = _build_description_ru(
                translated, row["source_url"], max_chars
            )
            await pool.execute(f"""
                UPDATE {Tables.EVENTS}
                SET description_ru = $1
                WHERE id = $2
            """, description_ru, row["id"])
            done += 1

    logger.info(f"TRANSLATE_DONE | переведено={done} / {len(rows)}")
    return done
