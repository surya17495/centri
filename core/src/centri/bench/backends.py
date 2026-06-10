"""centri-bench memory backends — native CENTRI vs the Letta escape hatch.

Both backends implement the same tiny protocol the harness drives:

  - ``ingest(persona)`` seeds the persona's event history and folds it into
    whatever memory representation the backend uses.
  - ``brief(cue, repo_id)`` returns the assembled brief text for the cold-start
    cue — the artifact every metric scores. centri-bench.md requires this be the
    *same* cue-driven path used in production, not a benchmark-only shortcut.

The native backend uses the real :class:`~centri.consolidation.Consolidator` +
:class:`~centri.memory_graph.MemoryGraph` +
:class:`~centri.memory_brief.MemoryBriefAssembler`. The Letta backend uses
:class:`~centri.letta_memory_store.LettaMemoryStore` (archival prose, no typed
supersession) and assembles a brief by retrieving against the cue — the honest
representation of what a prose-archival agent can re-inject.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Optional

from centri.bench.personas import Persona
from centri.consolidation import Consolidator
from centri.db import Database
from centri.letta_memory_store import LettaMemoryStore
from centri.memory_brief import MemoryBriefAssembler
from centri.memory_graph import MemoryGraph


class _DBMixin:
    async def _seed_ledger(self, db: Database, persona: Persona) -> None:
        for ev in persona.events:
            await db.append_event(
                event_id=ev["id"],
                type=ev["type"],
                source=ev.get("source", "bench"),
                ts=ev["ts"],
                repo_id=ev.get("repo_id"),
                payload=ev.get("payload", {}),
            )


class NativeBackend(_DBMixin):
    """CENTRI native: typed graph + supersession + cue-driven assembly."""

    name = "centri-native"

    def __init__(self) -> None:
        self._db: Optional[Database] = None
        self._graph: Optional[MemoryGraph] = None

    async def ingest(self, persona: Persona) -> None:
        tmp = tempfile.mkdtemp()
        self._db = Database(Path(tmp) / f"{persona.key}.db")
        self._graph = MemoryGraph(self._db)
        await self._graph.ensure_tables()
        await self._seed_ledger(self._db, persona)
        worker = Consolidator(self._db, self._graph)
        # Re-derive purely from the ledger — proves re-derivability and is the
        # production consolidation path.
        await worker.rebuild_from_events()

    async def brief(self, cue: str, repo_id: Optional[str]) -> str:
        assert self._graph is not None
        section = await MemoryBriefAssembler(self._graph).assemble(cue, repo_id=repo_id)
        return section.render()

    async def close(self) -> None:
        if self._db:
            await self._db.close()


class LettaBackend(_DBMixin):
    """Escape hatch: Letta-style archival prose, no typed supersession."""

    def __init__(self) -> None:
        self._db: Optional[Database] = None
        self._store: Optional[LettaMemoryStore] = None

    @property
    def name(self) -> str:
        mode = self._store.mode() if self._store else "local_projection"
        return f"letta-adapter[{mode}]"

    async def ingest(self, persona: Persona) -> None:
        tmp = tempfile.mkdtemp()
        self._db = Database(Path(tmp) / f"{persona.key}.db")
        self._store = LettaMemoryStore(self._db)
        await self._seed_ledger(self._db, persona)
        await self._store.rebuild_from_events()

    async def brief(self, cue: str, repo_id: Optional[str]) -> str:
        assert self._store is not None
        # Archival retrieval against the cue — Letta's actual injection shape.
        # No supersession: stale and current notes both come back.
        hits = await self._store.search_facts(cue, limit=12)
        if not hits:
            # Broaden: pull recent archival notes so the brief isn't empty.
            hits = await self._store.search_facts("", limit=12)
        lines = [f"  - {h.text} [{h.source_event_id or 'no-receipt'}]" for h in hits]
        return "Letta archival recall:\n" + "\n".join(lines)

    async def close(self) -> None:
        if self._db:
            await self._db.close()
