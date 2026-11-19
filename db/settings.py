"""Настройки бота в БД (таблица bot_settings), меняемые из админки без деплоя.

Приоритет: значение из БД → значение из config.yaml → безопасный запасной вариант. При ошибке БД режим категорий
откатывается на 'cards' (старое поведение), бот не падает. Читается с коротким кешем: на каждый тап SQL не нужен.
"""
from __future__ import annotations

import time

from loguru import logger

from db.tables import Tables
from shared.config import config

KEY_CATEGORY_MODE = 'ui.category_mode'

# cards — карточки сразу (как раньше); window_admin — окно со списком только админу; window — окно всем.
CATEGORY_MODES = ('cards', 'window_admin', 'window')
DEFAULT_CATEGORY_MODE = 'cards'
CACHE_TTL_SECONDS = 15

# key → (значение или None, момент чтения)
_cache: dict[str, tuple[str | None, float]] = {}


def normalize_mode(value) -> str | None:
    """'Window' / ' cards ' → допустимый режим, всё остальное → None."""
    mode = str(value).strip().lower() if value is not None else ''
    return mode if mode in CATEGORY_MODES else None


def config_category_mode() -> str:
    """Режим по умолчанию из config.yaml (ui.category_mode); мусор или пусто → 'cards'."""
    return normalize_mode((config.get('ui') or {}).get('category_mode')) or DEFAULT_CATEGORY_MODE


def clear_cache() -> None:
    _cache.clear()


async def get_setting(pool, key: str, use_cache: bool = True) -> str | None:
    """Значение из БД или None, если ключа нет. Ошибки БД пробрасываются."""
    now = time.monotonic()
    hit = _cache.get(key)
    if use_cache and hit and now - hit[1] < CACHE_TTL_SECONDS:
        return hit[0]
    value = await pool.fetchval(f'SELECT value FROM {Tables.BOT_SETTINGS} WHERE key = $1', key)
    _cache[key] = (value, now)
    return value


async def get_setting_row(pool, key: str):
    """Строка (value, updated_at, updated_by) без кеша — для экрана админки; None, если ключа нет."""
    return await pool.fetchrow(
        f'SELECT value, updated_at, updated_by FROM {Tables.BOT_SETTINGS} WHERE key = $1', key)


async def set_setting(pool, key: str, value: str, updated_by: int | None) -> None:
    await pool.execute(
        f"""INSERT INTO {Tables.BOT_SETTINGS} (key, value, updated_at, updated_by)
            VALUES ($1, $2, now(), $3)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now(), updated_by = EXCLUDED.updated_by""",
        key, value, updated_by)
    _cache.pop(key, None)


async def delete_setting(pool, key: str) -> None:
    await pool.execute(f'DELETE FROM {Tables.BOT_SETTINGS} WHERE key = $1', key)
    _cache.pop(key, None)


async def get_category_mode(pool) -> str:
    """Текущий режим категорий: БД → config.yaml. Любая ошибка БД или повреждённое значение → безопасный режим."""
    try:
        raw = await get_setting(pool, KEY_CATEGORY_MODE)
    except Exception as e:
        logger.error(f'SETTINGS | не прочитать {KEY_CATEGORY_MODE}, откат на {DEFAULT_CATEGORY_MODE}: {type(e).__name__}: {e}')
        return DEFAULT_CATEGORY_MODE
    if raw is None:
        return config_category_mode()
    mode = normalize_mode(raw)
    if mode is None:
        logger.error(f'SETTINGS | повреждённое значение {KEY_CATEGORY_MODE}={raw!r}, откат на {DEFAULT_CATEGORY_MODE}')
        return DEFAULT_CATEGORY_MODE
    return mode


async def resolve_category_mode(pool, user_id: int | None) -> str:
    """Что показать этому пользователю: 'cards' или 'window'. window_admin даёт окно только админу."""
    mode = await get_category_mode(pool)
    if mode == 'window':
        return 'window'
    if mode == 'window_admin' and user_id is not None and user_id == config['telegram']['admin_user_id']:
        return 'window'
    return 'cards'
