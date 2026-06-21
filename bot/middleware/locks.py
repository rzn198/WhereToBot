# bot/middleware/locks.py
# Per-user lock guarding against data races when concurrent_updates(True)

import time
import asyncio
from dataclasses import dataclass, field
from loguru import logger

# 24 hours of inactivity — after this, an entry may be removed by cleanup
TTL_SECONDS = 24 * 3600


@dataclass
class _LockEntry:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_used: float = field(default_factory=time.monotonic)


_user_locks: dict[int, _LockEntry] = {}

_locks_guard = asyncio.Lock()


async def get_user_lock(user_id: int) -> asyncio.Lock:
    """
    ⚠️ IMPORTANT — DO NOT ACQUIRE THIS LOCK ANYWHERE ELSE BESIDES
    bot/main.py: _session_middleware (group=-1) and _release_lock_middleware
    (group=999).

    If you need to guard a specific handler against concurrent execution,
    create a SEPARATE Lock for that purpose — do not reuse this one.
    """
    async with _locks_guard:
        entry = _user_locks.get(user_id)
        if entry is None:
            entry = _LockEntry()
            _user_locks[user_id] = entry
        entry.last_used = time.monotonic()
        return entry.lock


async def cleanup_stale_locks() -> None:

    now = time.monotonic()

    async with _locks_guard:
        stale_keys = [
            user_id
            for user_id, entry in _user_locks.items()
            if not entry.lock.locked() and (now - entry.last_used) > TTL_SECONDS
        ]
        for user_id in stale_keys:
            del _user_locks[user_id]

    if stale_keys:
        logger.debug(
            f"LOCKS_CLEANED | removed={len(stale_keys)} | "
            f"remaining={len(_user_locks)}"
        )
