"""CENTRI scheduler — timed ambient behavior.

Phase 0 left two TODOs in ``_tick``: a "nightly synthesis placeholder" and
"stale task detection". Phase 2 fills both:

  - **Consolidation ("sleep cycle").** Each tick drains the events appended since
    the last run and folds them into the typed memory graph via the
    :class:`~centri.consolidation.Consolidator`. Synthesis runs in the background
    and never blocks an interactive response (the hot/warm/cold tiering of
    ``docs/memory-architecture.md``).
  - **Dormancy detection.** Open loops untouched for ``dormancy_days`` surface
    exactly one yes/no line in the next briefing and are then marked dormant — the
    single piece of spoonfeeding the system is allowed to ask of the user.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Scheduler:
    """Periodic polling, stale detection, briefs, memory synthesis."""

    def __init__(
        self,
        db: Any,
        jobs: Any,
        memory: Any,
        observability: Any,
        consolidator: Any = None,
        memory_graph: Any = None,
        event_bus: Any = None,
        dormancy_days: float = 7.0,
        ingestor: Any = None,
        ingest_db_path: str = "",
    ):
        self._db = db
        self._jobs = jobs
        self._memory = memory
        self._observability = observability
        self._consolidator = consolidator
        self._graph = memory_graph
        self._event_bus = event_bus
        self._dormancy_days = dormancy_days
        self._ingestor = ingestor
        self._ingest_db_path = ingest_db_path
        self._task: Optional[asyncio.Task] = None
        self._interval = 30.0
        # High-water mark: only consolidate events newer than this timestamp.
        self._last_consolidated_ts: str = ""

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
        # Ingestion adapters — tail external session stores into the spine
        # before consolidation so ingested events fold in the same pass.
        await self.run_ingestion()
        # Consolidation ("sleep cycle") — fold new events into typed memory.
        await self.run_consolidation()
        # Dormancy detection — surface stale open loops once.
        await self.detect_dormant_loops()
        # Health snapshot
        health = await self._observability.health_snapshot(
            self._db, self._memory, type("mock", (), {"health": lambda: []})(), self._jobs, self
        )
        logger.debug("Health: db=%s memory=%s jobs=%s", health.db, health.memory, health.jobs)

    # ------------------------------------------------------------------
    # Ingestion (3b.3)
    # ------------------------------------------------------------------
    async def run_ingestion(self) -> int:
        """Tail the configured external opencode.db into the spine.

        Idempotent and incremental (the ingestor keeps a per-source high-water
        mark), so calling it every tick is cheap and never duplicates events.
        Returns the count ingested this pass.
        """
        if self._ingestor is None or not self._ingest_db_path:
            return 0
        try:
            result = await self._ingestor.ingest(self._ingest_db_path)
            return int(result.get("ingested", 0))
        except Exception:
            logger.debug("Ingestion pass failed", exc_info=True)
            return 0

    # ------------------------------------------------------------------
    # Consolidation
    # ------------------------------------------------------------------
    async def run_consolidation(self) -> int:
        """Fold events appended since the last run into the typed graph.

        Returns the number of typed objects written this pass. Idempotent across
        passes because supersession collapses repeats, but the high-water mark
        keeps each pass cheap.
        """
        if self._consolidator is None:
            return 0
        import json

        try:
            rows = await self._db.recent_events(limit=5000)
        except Exception:
            logger.debug("Consolidation event read failed", exc_info=True)
            return 0

        # recent_events is newest-first; replay oldest-first and skip anything at
        # or before the high-water mark.
        rows = list(reversed(rows))
        fresh: List[dict] = []
        newest_ts = self._last_consolidated_ts
        for row in rows:
            ts = row.get("ts") or ""
            if self._last_consolidated_ts and ts <= self._last_consolidated_ts:
                continue
            raw = row.get("payload_json")
            try:
                payload = json.loads(raw) if isinstance(raw, str) else (raw or {})
            except (TypeError, ValueError):
                payload = {}
            fresh.append(
                {
                    "id": row.get("id"),
                    "type": row.get("type"),
                    "repo_id": row.get("repo_id"),
                    "payload": payload,
                }
            )
            if ts > newest_ts:
                newest_ts = ts

        if not fresh:
            return 0
        written = await self._consolidator.consume_events(fresh)
        self._last_consolidated_ts = newest_ts
        return written

    # ------------------------------------------------------------------
    # Dormancy detection
    # ------------------------------------------------------------------
    async def detect_dormant_loops(self) -> List[str]:
        """Surface one yes/no line per newly-dormant open loop, then mark it.

        Returns the loop ids that were surfaced this pass. A loop is surfaced at
        most once: after the line is emitted, ``dormancy_asked_at`` is set and the
        loop drops to ``dormant`` so it is neither nagged again nor lost.
        """
        if self._graph is None:
            return []
        cutoff = (datetime.now(timezone.utc) - timedelta(days=self._dormancy_days)).isoformat()
        surfaced: List[str] = []
        try:
            from centri.memory_graph import LOOP_OPEN

            loops = await self._graph.open_loops(states=[LOOP_OPEN])
        except Exception:
            logger.debug("Dormancy scan failed", exc_info=True)
            return []
        for loop in loops:
            if loop.dormancy_asked_at:
                continue
            if (loop.last_touched_at or loop.created_at) <= cutoff:
                await self._graph.mark_dormancy_asked(loop.id)
                await self._emit_dormancy(loop)
                surfaced.append(loop.id)
        return surfaced

    async def _emit_dormancy(self, loop: Any) -> None:
        line = f"Still pursuing \"{loop.intent}\", or park it?"
        payload = {"loop_id": loop.id, "intent": loop.intent, "question": line, "kind": "dormancy"}
        ts = _now()
        try:
            await self._db.append_event(
                event_id=f"dormancy-{loop.id}-{ts}",
                type="notification.sent",
                source="runtime",
                ts=ts,
                repo_id=loop.repo_id,
                payload=payload,
            )
        except Exception:
            logger.debug("Dormancy ledger write failed", exc_info=True)
        if self._event_bus is not None:
            try:
                await self._event_bus.publish(
                    {"type": "notification.sent", "ts": ts, "source": "runtime",
                     "payload": payload, "summary": line}
                )
            except Exception:
                logger.debug("Dormancy publish failed", exc_info=True)
