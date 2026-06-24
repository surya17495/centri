"""CENTRI memory store interface — the derived, re-derivable memory layer.

Design principle (stated everywhere in CENTRI): **events are the source of
truth; memory is a derived, re-derivable index.** Nothing in a ``MemoryStore`` is
authoritative — :meth:`MemoryStore.rebuild_from_events` must be able to discard
all memory state and reconstruct it by replaying the event ledger.

The interface has four parts:
  - **Core memory blocks** — a small set of named, always-resident blocks the
    agent keeps in working context: ``active_project``, ``open_loops``,
    ``priorities``, ``people``.
  - **Archival facts** — an unbounded, searchable store of discrete facts.
  - **Synthesis hook** — :meth:`consume_events` folds a batch of events into
    memory entries (full synthesis is Phase 2; the Phase 0 impl is minimal).
  - **Rebuild** — :meth:`rebuild_from_events` re-derives everything from the spine.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

CORE_BLOCKS = ("active_project", "open_loops", "priorities", "people")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ArchivalFact:
    """A discrete, searchable memory fact derived from the event spine."""

    id: str
    text: str
    source_event_id: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    created_at: str = field(default_factory=_now)


class MemoryStore(ABC):
    """Derived, re-derivable memory over the event spine."""

    # -- core memory blocks -------------------------------------------------
    @abstractmethod
    async def get_block(self, name: str) -> str:
        """Return the named core block's content (empty string if unset)."""

    @abstractmethod
    async def set_block(self, name: str, content: str) -> None:
        """Replace the named core block's content."""

    @abstractmethod
    async def all_blocks(self) -> Dict[str, str]:
        """Return all core blocks as a name -> content mapping."""

    # -- archival facts -----------------------------------------------------
    @abstractmethod
    async def insert_fact(self, fact: ArchivalFact) -> None:
        """Store an archival fact."""

    @abstractmethod
    async def search_facts(self, query: str, limit: int = 10) -> List[ArchivalFact]:
        """Return archival facts matching ``query`` (substring match in Phase 0)."""

    # -- synthesis ----------------------------------------------------------
    @abstractmethod
    async def consume_events(self, events: List[Dict[str, Any]]) -> int:
        """Fold a batch of events into memory entries. Returns entries written.

        Phase 0 is intentionally minimal; the synthesis worker arrives in Phase 2.
        """

    # -- re-derivation ------------------------------------------------------
    @abstractmethod
    async def rebuild_from_events(self) -> int:
        """Discard derived memory and rebuild it from the full event ledger.

        Returns the number of entries written. This is what makes memory a
        *derived* index: the spine is authoritative.
        """


class SqliteMemoryStore(MemoryStore):
    """Minimal MemoryStore backed by the existing SQLite db layer.

    Core blocks and archival facts live in two tables created on first use. The
    Phase 0 synthesis is deliberately simple — it promotes a handful of event
    types into facts and tracks the active project — but it round-trips through
    the spine so :meth:`rebuild_from_events` works end to end.
    """

    def __init__(self, db: Any):
        self._db = db

    async def _ensure_tables(self) -> None:
        await self._db._execute(
            """CREATE TABLE IF NOT EXISTS memory_blocks (
                name TEXT PRIMARY KEY,
                content TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            )"""
        )
        await self._db._execute(
            """CREATE TABLE IF NOT EXISTS memory_facts (
                id TEXT PRIMARY KEY,
                text TEXT NOT NULL,
                source_event_id TEXT,
                tags TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )"""
        )

    # -- core memory blocks -------------------------------------------------
    async def get_block(self, name: str) -> str:
        await self._ensure_tables()
        rows = await self._db._execute("SELECT content FROM memory_blocks WHERE name = ?", (name,))
        row = rows[0] if rows else None
        return row["content"] if row else ""

    async def set_block(self, name: str, content: str) -> None:
        await self._ensure_tables()
        await self._db._execute(
            """INSERT INTO memory_blocks (name, content, updated_at) VALUES (?,?,?)
               ON CONFLICT(name) DO UPDATE SET content=excluded.content, updated_at=excluded.updated_at""",
            (name, content, _now()),
        )

    async def all_blocks(self) -> Dict[str, str]:
        await self._ensure_tables()
        rows = await self._db._execute("SELECT name, content FROM memory_blocks")
        stored = {row["name"]: row["content"] for row in rows}
        return {name: stored.get(name, "") for name in CORE_BLOCKS}

    # -- archival facts -----------------------------------------------------
    async def insert_fact(self, fact: ArchivalFact) -> None:
        await self._ensure_tables()
        await self._db._execute(
            "INSERT OR REPLACE INTO memory_facts (id, text, source_event_id, tags, created_at) VALUES (?,?,?,?,?)",
            (fact.id, fact.text, fact.source_event_id, ",".join(fact.tags), fact.created_at),
        )

    async def search_facts(self, query: str, limit: int = 10) -> List[ArchivalFact]:
        await self._ensure_tables()
        rows = await self._db._execute(
            "SELECT * FROM memory_facts WHERE text LIKE ? ORDER BY created_at DESC LIMIT ?",
            (f"%{query}%", limit),
        )
        return [
            ArchivalFact(
                id=row["id"],
                text=row["text"],
                source_event_id=row["source_event_id"],
                tags=[t for t in (row["tags"] or "").split(",") if t],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    # -- synthesis ----------------------------------------------------------
    async def consume_events(self, events: List[Dict[str, Any]]) -> int:
        await self._ensure_tables()
        written = 0
        for event in events:
            etype = event.get("type", "")
            eid = event.get("id") or event.get("event_id")
            payload = event.get("payload") or {}
            if etype == "task.started":
                desc = payload.get("description") or event.get("description") or ""
                if desc:
                    await self.set_block("active_project", desc)
                    await self.insert_fact(
                        ArchivalFact(id=f"fact-{eid}", text=f"Started: {desc}", source_event_id=eid, tags=["task"])
                    )
                    written += 1
            elif etype in ("task.completed", "task.failed"):
                summary = payload.get("summary") or event.get("summary") or etype
                await self.insert_fact(
                    ArchivalFact(id=f"fact-{eid}", text=summary, source_event_id=eid, tags=["task", etype.split(".")[-1]])
                )
                written += 1
        return written

    # -- re-derivation ------------------------------------------------------
    async def rebuild_from_events(self) -> int:
        await self._ensure_tables()
        await self._db._execute("DELETE FROM memory_facts")
        await self._db._execute("DELETE FROM memory_blocks")
        rows = await self._db.recent_events(limit=100_000)
        # Ledger rows store the body as a JSON string in ``payload_json``; decode
        # it back into the in-memory event shape consume_events expects.
        events: List[Dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(row.get("payload_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                payload = {}
            events.append({"id": row.get("id"), "type": row.get("type"), "payload": payload})
        # recent_events returns newest-first; replay oldest-first.
        return await self.consume_events(list(reversed(events)))
