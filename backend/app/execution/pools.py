"""Concurrency pools for job execution.

Each job kind maps to a named resource pool (GPU-bound rendering gets a
single slot, cheap LLM calls get many).  :class:`PoolManager` enforces the
per-pool limits with ``asyncio`` semaphores and tracks how many slots are
currently held so the runner can decide which kinds are worth claiming and
tests can assert on concurrency.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

__all__ = ["POOL_LIMITS", "KIND_TO_POOL", "pool_for_kind", "PoolManager"]


#: Maximum concurrent jobs per pool.
POOL_LIMITS: dict[str, int] = {
    "render": 1,
    "tts": 4,
    "llm": 8,
    "downloads": 6,
    "upload": 2,
    "default": 4,
}

#: Explicit job-kind -> pool mapping.  ``pipeline.*`` kinds are handled by
#: :func:`pool_for_kind` (they all land in ``default``).
KIND_TO_POOL: dict[str, str] = {
    "stage.render": "render",
    "stage.voice": "tts",
    "stage.research": "llm",
    "stage.script": "llm",
    "stage.package": "llm",
    "stage.qc": "llm",
    "stage.visuals": "downloads",
    "stage.thumbnail": "downloads",
    "stage.publish": "upload",
    "stage.upload": "upload",
    "gate.a": "default",
    "gate.b": "default",
}


def pool_for_kind(kind: str) -> str:
    """Pool name for a job kind; unknown kinds fall back to ``default``."""
    pool = KIND_TO_POOL.get(kind)
    if pool is not None:
        return pool
    if kind.startswith("pipeline.") or kind.startswith("gate."):
        return "default"
    return "default"


class PoolManager:
    """Asyncio-semaphore gatekeeper for the named pools.

    Semaphores are created lazily inside the running event loop (and
    re-created if a different loop shows up, e.g. across tests) so a
    manager can be constructed synchronously before any loop exists.
    """

    def __init__(self, limits: dict[str, int] | None = None) -> None:
        self._limits: dict[str, int] = dict(POOL_LIMITS)
        if limits:
            self._limits.update(limits)
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._semaphore_loops: dict[str, asyncio.AbstractEventLoop] = {}
        self._in_use: dict[str, int] = {}

    # ------------------------------------------------------------------
    def limit(self, pool: str) -> int:
        return self._limits.get(pool, POOL_LIMITS["default"])

    def in_use(self, pool: str) -> int:
        """Number of slots of ``pool`` currently held."""
        return self._in_use.get(pool, 0)

    def available(self, pool: str) -> int:
        """Free slots of ``pool`` (never negative)."""
        return max(self.limit(pool) - self.in_use(pool), 0)

    def has_capacity(self, pool: str) -> bool:
        return self.in_use(pool) < self.limit(pool)

    # ------------------------------------------------------------------
    def _semaphore(self, pool: str) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        sem = self._semaphores.get(pool)
        if sem is None or self._semaphore_loops.get(pool) is not loop:
            sem = asyncio.Semaphore(self.limit(pool))
            self._semaphores[pool] = sem
            self._semaphore_loops[pool] = loop
        return sem

    @asynccontextmanager
    async def acquire(self, pool: str):
        """Hold one slot of ``pool`` for the duration of the block.

        Usage::

            async with pools.acquire("render"):
                ...  # at most POOL_LIMITS["render"] of these at once
        """
        sem = self._semaphore(pool)
        await sem.acquire()
        self._in_use[pool] = self._in_use.get(pool, 0) + 1
        try:
            yield pool
        finally:
            self._in_use[pool] = max(self._in_use.get(pool, 1) - 1, 0)
            sem.release()
