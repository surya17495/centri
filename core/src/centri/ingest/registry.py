"""CENTRI ingestion adapter registry + discovery + bootstrap (ROADMAP 3b.4).

The registry holds one :class:`~centri.ingest.base.MessageAdapter` per coding
agent (OpenCode, Claude Code, Cursor today). All adapters share the HWM /
idempotency / redaction / write core, so the registry only has to fan out:

  - **discover()** — probe each agent's well-known default paths (per platform)
    plus any configured overrides, returning what was found with cheap counts.
    Honest-unavailable: nothing found → empty list (or per-source reasons), never
    a fabricated result.
  - **bootstrap()** — run a full import across all discovered (or explicitly
    configured) sources. Because ingestion is high-water-mark based, *bootstrap is
    the first tick*: a fresh install's one-time import and the ambient tail are
    the same ``adapter.ingest(path)`` call; the only difference is the HWM starts
    empty. Bootstrap emits ``ingest.bootstrap.*`` progress events on the spine so
    the shell timeline shows discovery → per-source progress → done.

Per-agent path overrides / disables come from :class:`IngestConfig`, built from
``Settings`` (env-driven).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from centri.ingest.base import DiscoveredSource, MessageAdapter
from centri.ingest.claude_code import ClaudeCodeIngestor
from centri.ingest.cursor import CursorIngestor
from centri.ingest.opencode import OpenCodeIngestor

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# Adapter classes by agent name; order is the discovery / bootstrap order.
_ADAPTER_CLASSES = {
    "opencode": OpenCodeIngestor,
    "claude_code": ClaudeCodeIngestor,
    "cursor": CursorIngestor,
}


@dataclass
class IngestConfig:
    """Per-agent ingestion config: extra paths to probe, and which agents are off.

    ``extra_paths`` maps agent → list of override paths probed *in addition to*
    the platform defaults (a user whose store lives somewhere unusual). ``disabled``
    is a set of agent names to skip entirely (privacy / opt-out).
    """

    extra_paths: Dict[str, List[str]] = field(default_factory=dict)
    disabled: set[str] = field(default_factory=set)

    @classmethod
    def from_settings(cls, settings: Any) -> "IngestConfig":
        extra: Dict[str, List[str]] = {}
        disabled: set[str] = set()
        # Single configured opencode.db (3b.3 contract) is an extra path.
        oc = getattr(settings, "opencode_ingest_db", "") or ""
        if oc:
            extra.setdefault("opencode", []).append(oc)
        for agent in _ADAPTER_CLASSES:
            paths = getattr(settings, f"ingest_{agent}_paths", None)
            if paths:
                extra.setdefault(agent, []).extend(
                    [p.strip() for p in str(paths).split(",") if p.strip()]
                )
        disabled_raw = getattr(settings, "ingest_disabled_agents", None)
        if disabled_raw:
            disabled |= {a.strip() for a in str(disabled_raw).split(",") if a.strip()}
        return cls(extra_paths=extra, disabled=disabled)


class IngestRegistry:
    """Holds per-agent adapters; fans out discovery, ambient tail, and bootstrap."""

    def __init__(self, db: Any, event_bus: Any = None, config: Optional[IngestConfig] = None):
        self._db = db
        self._event_bus = event_bus
        self._config = config or IngestConfig()
        self._adapters: Dict[str, MessageAdapter] = {
            name: cls(db, event_bus=event_bus) for name, cls in _ADAPTER_CLASSES.items()
        }

    def adapter(self, agent: str) -> Optional[MessageAdapter]:
        return self._adapters.get(agent)

    @property
    def opencode(self) -> MessageAdapter:
        """The OpenCode adapter — preserves the 3b.3 ``runtime.opencode_ingestor``."""
        return self._adapters["opencode"]

    def active_agents(self) -> List[str]:
        return [a for a in self._adapters if a not in self._config.disabled]

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------
    def discover(self) -> List[DiscoveredSource]:
        """Probe every active agent's default + configured paths (read-only)."""
        found: List[DiscoveredSource] = []
        for agent in self.active_agents():
            adapter = self._adapters[agent]
            extra = self._config.extra_paths.get(agent, [])
            try:
                found.extend(adapter.discover(extra_paths=extra))
            except Exception:  # noqa: BLE001 — one bad adapter never sinks discovery
                logger.debug("discovery failed for %s", agent, exc_info=True)
        return found

    def discover_summary(self) -> Dict[str, Any]:
        sources = self.discover()
        available = [s for s in sources if s.available]
        total = sum(s.count or 0 for s in available)
        return {
            "sources": [s.as_dict() for s in sources],
            "available_count": len(available),
            "total_messages": total,
            "agents": self.active_agents(),
        }

    # ------------------------------------------------------------------
    # Bootstrap = first tick: full import across discovered/configured sources
    # ------------------------------------------------------------------
    async def bootstrap(
        self, sources: Optional[List[DiscoveredSource]] = None
    ) -> Dict[str, Any]:
        """Run a full import across all discovered (or supplied) sources.

        Emits progress events on the spine so the shell timeline reflects the
        bootstrap. Idempotent: a second bootstrap re-imports nothing because each
        source keeps its high-water mark.
        """
        if sources is None:
            sources = [s for s in self.discover() if s.available]

        await self._emit(
            "ingest.bootstrap.started",
            {"source_count": len(sources), "agents": self.active_agents()},
            summary=f"Memory bootstrap: importing from {len(sources)} discovered source(s)",
        )

        results: List[Dict[str, Any]] = []
        total_ingested = 0
        for s in sources:
            adapter = self._adapters.get(s.agent)
            if adapter is None or s.agent in self._config.disabled:
                continue
            result = await adapter.ingest(s.path, source=s.source or None)
            results.append(result)
            total_ingested += int(result.get("ingested", 0))
            await self._emit(
                "ingest.bootstrap.progress",
                {
                    "agent": s.agent,
                    "source": result.get("source"),
                    "ingested": result.get("ingested", 0),
                    "scanned": result.get("scanned", 0),
                    "available": result.get("available", False),
                },
                summary=(
                    f"Imported {result.get('ingested', 0)} message(s) from "
                    f"{s.agent} ({s.path})"
                ),
            )

        summary = {
            "imported": total_ingested,
            "source_count": len(results),
            "results": results,
        }
        await self._emit(
            "ingest.bootstrap.completed",
            summary,
            summary=f"Memory bootstrap done: imported {total_ingested} message(s) "
            f"from {len(results)} source(s)",
        )
        return summary

    async def _emit(self, event_type: str, payload: Dict[str, Any], summary: str = "") -> None:
        """Append a bootstrap progress event to the spine (and publish live)."""
        event_id = f"ingest-bootstrap:{event_type}:{_now()}"
        try:
            await self._db.append_event(
                event_id=event_id,
                type=event_type,
                source="ingest.bootstrap",
                ts=_now(),
                importance="normal",
                payload={**payload, "summary": summary},
            )
        except Exception:  # noqa: BLE001
            logger.debug("bootstrap event write failed for %s", event_type, exc_info=True)
        if self._event_bus is not None:
            try:
                await self._event_bus.publish(
                    {
                        "id": event_id,
                        "type": event_type,
                        "ts": _now(),
                        "source": "ingest.bootstrap",
                        "importance": "normal",
                        "payload": {**payload, "summary": summary},
                        "summary": summary,
                    }
                )
            except Exception:  # noqa: BLE001
                logger.debug("bootstrap event publish failed for %s", event_type, exc_info=True)
