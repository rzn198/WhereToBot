from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Iterable, TypeVar

from shared.config import config

DEFAULT_DETAIL_CONCURRENCY = 5

T = TypeVar('T')
R = TypeVar('R')


def get_detail_concurrency() -> int:
    cfg = config.get('event_collector', {}) or {}
    try:
        value = int(cfg.get('detail_concurrency', DEFAULT_DETAIL_CONCURRENCY))
    except (TypeError, ValueError):
        value = DEFAULT_DETAIL_CONCURRENCY
    return max(1, value)


async def gather_limited(
    items: Iterable[T],
    worker: Callable[[T], Awaitable[R]],
    limit: int | None = None,
) -> list[R]:
    sem = asyncio.Semaphore(limit or get_detail_concurrency())

    async def run(item: T) -> R:
        async with sem:
            return await worker(item)

    return list(await asyncio.gather(*(run(i) for i in items)))
