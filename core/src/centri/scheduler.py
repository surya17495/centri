"""CENTRI scheduler — timed ambient behavior."""

import asyncio
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


class Scheduler:
    """Periodic polling, stale detection, briefs, memory synthesis."""

    def __init__(self, db: Any, jobs: Any, memory: Any, observability: Any):
        self._db = db
        self._jobs = jobs
        self._memory = memory
        self._observability = observability
        self._task: Optional[asyncio.Task] = None
        self._interval = 30.0

    async def start(self) -> None:
        logger.info("Scheduler starting")
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._interval)
                await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Scheduler tick failed: %s", exc)

    async def _tick(self) -> None:
        # Poll running jobs
        await self._jobs.poll_once()
        # Stale task detection
        # Nightly synthesis placeholder
        # Health snapshot
        health = await self._observability.health_snapshot(
            self._db, self._memory, type("mock", (), {"health": lambda: []})(), self._jobs, self
        )
        logger.debug("Health: db=%s memory=%s jobs=%s", health.db, health.memory, health.jobs)
