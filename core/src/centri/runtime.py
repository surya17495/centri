"""CENTRI runtime — process lifespan supervisor."""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Runtime:
    """Boots and shuts down all CENTRI subsystems in order."""

    def __init__(self):
        self.event_bus: Any = None
        self.db: Any = None
        self.observability: Any = None
        self.accounts: Any = None
        self.memory: Any = None
        self.permissions: Any = None
        self.hands: Any = None
        self.jobs: Any = None
        self.scheduler: Any = None
        self.model_router: Any = None
        self.context_assembler: Any = None
        self.desktop: Any = None
        self.coordinator: Any = None
        self.notifier: Any = None
        self.hot_cache: Any = None
        self.briefing_builder: Any = None
        self.memory_graph: Any = None
        self.consolidator: Any = None
        self.opencode_ingestor: Any = None
        self.ingest_registry: Any = None
        self.opencode_config: Any = None
        self.models_catalog: Any = None
        self.memory_brief: Any = None
        self.proactive_brief: Any = None
        self._background_tasks: list[asyncio.Task] = []

    async def boot(self) -> None:
        from centri.config import get_settings
        from centri.db import Database
        from centri.model_router import ModelRouter
        from centri.memory import Memory
        from centri.context import ContextAssembler
        from centri.permissions import Permissions
        from centri.hands import Hands
        from centri.jobs import Jobs
        from centri.artifacts import Artifacts
        from centri.observability import Observability
        from centri.scheduler import Scheduler
        from centri.accounts import Accounts
        from centri.coordinator import Coordinator
        from centri.context_cache import HotContextCache
        from centri.briefing import BriefingBuilder
        from centri.event_bus import EventBus
        from centri.memory_graph import MemoryGraph
        from centri.consolidation import Consolidator
        from centri.ingest import IngestConfig, IngestRegistry
        from centri.memory_brief import MemoryBriefAssembler, ProactiveBriefBuilder
        from centri.curation import Curator
        from centri.models_catalog import ModelsCatalog
        from centri.opencode_config import OpenCodeConfig

        settings = get_settings()
        logger.info("CENTRI booting...")

        # 0. Event bus
        self.event_bus = EventBus()

        # 1. DB
        self.db = Database()

        # 2. Observability
        self.observability = Observability()

        # 3. Accounts
        self.accounts = Accounts(settings)

        # 4. Memory
        self.memory = Memory(self.db, event_bus=self.event_bus)

        # 5. Permissions
        self.permissions = Permissions(settings)

        # 6. Hot context cache + briefing builder
        self.hot_cache = HotContextCache()
        self.briefing_builder = BriefingBuilder()

        # 6b. Phase 2 typed memory graph + consolidation worker + cue injection
        self.memory_graph = MemoryGraph(self.db)
        await self.memory_graph.ensure_tables()
        self.consolidator = Consolidator(self.db, self.memory_graph, event_bus=self.event_bus)
        # Ingestion adapter registry (3b.4): OpenCode + Claude Code + Cursor share
        # one HWM/idempotency/redaction core. opencode_ingestor stays the OpenCode
        # adapter so the 3b.3 endpoint + scheduler contract is unchanged.
        self.ingest_registry = IngestRegistry(
            self.db,
            event_bus=self.event_bus,
            config=IngestConfig.from_settings(settings),
        )
        self.opencode_ingestor = self.ingest_registry.opencode
        # Single-LLM-config (3b.5): read-only view of OpenCode's provider config
        # /auth, reused by the model router as a key fallback and surfaced (key
        # material stripped) at GET /providers/discovered. models.dev is the
        # UI-display model catalog (catalog only; LiteLLM remains the transport).
        oc_extra = [
            p.strip() for p in (settings.ingest_opencode_paths or "").split(",") if p.strip()
        ]
        self.opencode_config = OpenCodeConfig(extra_dirs=oc_extra or None)
        self.models_catalog = ModelsCatalog()
        self.memory_brief = MemoryBriefAssembler(self.memory_graph)
        self.proactive_brief = ProactiveBriefBuilder(self.db, self.memory_graph)
        # 3c.0 deterministic context curation — the live brief path. Model
        # router is attached after it is constructed (below) for the
        # honest-unavailable cue-expansion seam.
        self.curator = Curator(self.memory_graph, settings=settings)

        # 7. Hands
        self.hands = Hands(settings, self.db)

        # 8. Jobs
        self.jobs = Jobs(self.db, self.hands, self.permissions, self.event_bus, memory=self.memory)

        # 9. Scheduler (carries the consolidation worker + dormancy detection)
        self.scheduler = Scheduler(
            self.db,
            self.jobs,
            self.memory,
            self.observability,
            consolidator=self.consolidator,
            memory_graph=self.memory_graph,
            event_bus=self.event_bus,
            ingestor=self.opencode_ingestor,
            ingest_db_path=settings.opencode_ingest_db,
        )

        # 10. Model router — reuses OpenCode's provider auth as a key fallback.
        self.model_router = ModelRouter(opencode_config=self.opencode_config)

        # 11. Desktop context — Tauri shell lands in Phase 1; run without it.
        self.desktop = None

        # 12. Context assembler
        self.context_assembler = ContextAssembler(self.db, self.memory, self.desktop)

        # 13. Artifacts
        artifacts = Artifacts(self.db)

        # 14. Coordinator
        self.coordinator = Coordinator(
            db=self.db,
            model_router=self.model_router,
            memory=self.memory,
            context_assembler=self.context_assembler,
            permissions=self.permissions,
            hands=self.hands,
            jobs=self.jobs,
            artifacts=artifacts,
            desktop=self.desktop,
            event_bus=self.event_bus,
            hot_cache=self.hot_cache,
            briefing_builder=self.briefing_builder,
            memory_brief=self.memory_brief,
            curator=self.curator,
        )

        # 15. Wire event bus -> hot cache
        if self.hot_cache:
            self._background_tasks.append(asyncio.create_task(self._event_cache_loop()))

        if self.notifier:
            self._background_tasks.append(asyncio.create_task(self._notification_event_loop()))

        # 16. Recover jobs
        await self.jobs.recover_on_boot()

        # 17. Start scheduler
        await self.scheduler.start()

        logger.info("CENTRI booted.")

    async def shutdown(self) -> None:
        logger.info("CENTRI shutting down...")
        if self.scheduler:
            await self.scheduler.stop()
        for task in self._background_tasks:
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        self._background_tasks.clear()
        if self.db:
            await self.db.close()
        logger.info("CENTRI shut down.")

    async def _event_cache_loop(self) -> None:
        """Pump event bus into hot cache continually."""
        if not self.event_bus or not self.hot_cache:
            return
        try:
            q = await self.event_bus.subscribe()
            while True:
                event = await q.get()
                try:
                    await self.hot_cache.apply_event(event)
                except Exception:
                    pass
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning("Event cache loop error: %s", exc)

    async def _notification_event_loop(self) -> None:
        """Pump event bus to the active notifier for proactive messages."""
        if not self.event_bus or not self.notifier:
            return
        try:
            q = await self.event_bus.subscribe()
            while True:
                event = await q.get()
                try:
                    ev_type = event.get("type", "")
                    payload = event.get("payload", {})

                    def _get(key: str) -> Any:
                        return event.get(key, payload.get(key))

                    if ev_type == "approval.requested":
                        await self.notifier.notify_approval_request(
                            approval_id=str(_get("approval_id")),
                            label=str(_get("label") or ""),
                            risk=str(_get("risk") or "medium"),
                        )
                        if self.db:
                            await self.db.append_event(
                                event_id=f"notify-approval-{_now()}",
                                type="notification.sent",
                                source="runtime",
                                ts=_now(),
                                task_id=str(_get("task_id") or "") or None,
                                payload={"kind": "approval.requested", "approval_id": str(_get("approval_id") or "")},
                            )
                    elif ev_type == "task.completed":
                        await self.notifier.notify_task_completed(
                            task_id=str(_get("task_id")),
                            summary=str(_get("summary") or ""),
                        )
                        if self.db:
                            await self.db.append_event(
                                event_id=f"notify-task-{_now()}",
                                type="notification.sent",
                                source="runtime",
                                ts=_now(),
                                task_id=str(_get("task_id") or "") or None,
                                payload={"kind": "task.completed", "summary": str(_get("summary") or "")},
                            )
                    elif ev_type == "task.failed":
                        await self.notifier.notify_task_failed(
                            task_id=str(_get("task_id")),
                            error=str(_get("error") or ""),
                        )
                        if self.db:
                            await self.db.append_event(
                                event_id=f"notify-failed-{_now()}",
                                type="notification.sent",
                                source="runtime",
                                ts=_now(),
                                task_id=str(_get("task_id") or "") or None,
                                payload={"kind": "task.failed", "error": str(_get("error") or "")},
                            )
                except Exception:
                    pass
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning("Notification event loop error: %s", exc)
