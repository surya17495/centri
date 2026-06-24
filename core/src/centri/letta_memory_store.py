"""LettaMemoryStore — the escape-hatch adapter (docs/memory-architecture.md).

``MemoryStore`` is an ABC precisely so the memory backend is swappable. This
module is the head-to-head comparand for ``centri-bench``: the same harness, the
same seeded event history, a different store. The architecture doc commits to
running native-vs-Letta and swapping if Letta wins decisively — and because
events are the source of truth, that swap is a re-derivation, not a rewrite.

**Honest sandbox accounting.** Self-hosting Letta is Docker + Postgres +
pgvector (a ~2GB-RAM service), which is intentionally absent from the build
sandbox. So this adapter:

  - Talks to a Letta server over HTTP when ``letta_url`` is configured.
  - Otherwise reports ``available() is False`` and degrades to a *local
    projection* of Letta's archival model — freeform notes in a single SQLite
    table, no typed supersession.

That degraded mode is not a fake Letta; it is the honest representation of what
Letta's archival memory *is* at the storage layer (prose notes you retrieve),
which is exactly the ~20% commodity the architecture doc says Letta saves and
the ~80% (typed supersession, prospective triggers, cue-driven assembly) it does
not. The bench reports which mode ran so the comparison is never silently faked.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from centri.config import get_settings
from centri.memory_store import ArchivalFact, MemoryStore

logger = logging.getLogger(__name__)

try:
    import httpx
except ModuleNotFoundError:
    httpx = None  # type: ignore[assignment]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class LettaMemoryStore(MemoryStore):
    """A ``MemoryStore`` backed by Letta's archival model (or a local projection)."""

    def __init__(self, db: Any, letta_client: Any = None):
        self._db = db
        settings = get_settings()
        self._settings = settings
        self._base = (settings.letta_url or "").rstrip("/")
        self._api_key = settings.letta_api_key or ""
        self._agent_id = settings.letta_agent_id
        # ``letta_client`` is the injectable HTTP transport (LettaHTTPClient or a
        # test double). When a Letta server is configured but no client was
        # passed, build a real one lazily so production wiring needs no glue.
        self._client = letta_client
        if self._client is None and self._base:
            self._client = self._build_http_client()
        self._ready = False
        self._http_agent_ready = False

    def _build_http_client(self) -> Any:
        try:
            from centri.letta_http import LettaHTTPClient
        except Exception:  # pragma: no cover - import guard
            return None
        try:
            return LettaHTTPClient(
                base_url=self._base,
                api_key=self._api_key,
                model=self._settings.letta_model,
                model_endpoint=self._settings.letta_model_endpoint,
                embedding_model=self._settings.letta_embedding_model,
                embedding_endpoint=self._settings.letta_embedding_endpoint,
                embedding_dim=self._settings.letta_embedding_dim,
            )
        except Exception as exc:  # pragma: no cover - SDK missing/misconfigured
            logger.warning("Letta HTTP client unavailable, using local projection: %s", exc)
            return None

    def available(self) -> bool:
        return self._client is not None

    def mode(self) -> str:
        return "letta_http" if self.available() else "local_projection"

    async def _ensure_http_agent(self) -> None:
        """Create the remote bench agent once per ingest (HTTP mode only)."""
        if self._http_agent_ready or self._client is None:
            return
        import asyncio

        await asyncio.to_thread(self._client.ensure_agent)
        self._http_agent_ready = True

    async def _ensure(self) -> None:
        if self._ready:
            return
        await self._db._execute(
            """CREATE TABLE IF NOT EXISTS letta_blocks (
                name TEXT PRIMARY KEY, content TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL
            )"""
        )
        await self._db._execute(
            """CREATE TABLE IF NOT EXISTS letta_archival (
                id TEXT PRIMARY KEY, text TEXT NOT NULL, source_event_id TEXT,
                tags TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
            )"""
        )
        self._ready = True

    # -- core blocks --------------------------------------------------------
    async def get_block(self, name: str) -> str:
        await self._ensure()
        rows = await self._db._execute("SELECT content FROM letta_blocks WHERE name = ?", (name,))
        row = rows[0] if rows else None
        return row["content"] if row else ""

    async def set_block(self, name: str, content: str) -> None:
        await self._ensure()
        await self._db._execute(
            "INSERT INTO letta_blocks (name, content, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET content=excluded.content, updated_at=excluded.updated_at",
            (name, content, _now()),
        )

    async def all_blocks(self) -> Dict[str, str]:
        await self._ensure()
        rows = await self._db._execute("SELECT name, content FROM letta_blocks")
        return {row["name"]: row["content"] for row in rows}

    # -- archival -----------------------------------------------------------
    async def insert_fact(self, fact: ArchivalFact) -> None:
        if self._client is not None:
            import asyncio

            await self._ensure_http_agent()
            await asyncio.to_thread(self._client.insert_passage, fact.text, fact.tags)
            return
        await self._ensure()
        await self._db._execute(
            "INSERT OR REPLACE INTO letta_archival (id, text, source_event_id, tags, created_at) VALUES (?,?,?,?,?)",
            (fact.id, fact.text, fact.source_event_id, ",".join(fact.tags), fact.created_at),
        )

    async def search_facts(self, query: str, limit: int = 10) -> List[ArchivalFact]:
        if self._client is not None:
            import asyncio

            await self._ensure_http_agent()
            rows = await asyncio.to_thread(self._client.search_passages, query, limit)
            # Real Letta archival is semantic prose retrieval with no typed
            # supersession — superseded and current passages both come back,
            # exactly the accumulation failure the bench surfaces.
            return [
                ArchivalFact(id=pid or f"passage-{i}", text=text, source_event_id=None, tags=[])
                for i, (text, pid) in enumerate(rows)
            ]
        await self._ensure()
        # Local-projection fallback: substring/lexical match, also no typed
        # supersession — the honest storage-layer model of Letta's archival.
        terms = [t for t in query.lower().split() if len(t) > 2][:4]
        like = "%" + "%".join(terms) + "%" if terms else "%"
        rows = await self._db._execute(
            "SELECT * FROM letta_archival WHERE LOWER(text) LIKE ? ORDER BY created_at DESC LIMIT ?",
            (like, limit),
        )
        return [
            ArchivalFact(
                id=r["id"], text=r["text"], source_event_id=r["source_event_id"],
                tags=[t for t in (r["tags"] or "").split(",") if t], created_at=r["created_at"],
            )
            for r in rows
        ]

    # -- synthesis ----------------------------------------------------------
    async def consume_events(self, events: List[Dict[str, Any]]) -> int:
        """Fold events into archival prose notes — no typed supersession.

        Letta's model stores experiences as text the agent later retrieves. We
        flatten each typed hint into a prose note. Crucially we do NOT supersede:
        a renamed service leaves both notes in the store, so retrieval returns a
        contradictory pair — the accumulation failure the native store avoids.
        """
        await self._ensure()
        written = 0
        for ev in events:
            eid = ev.get("id") or ev.get("event_id")
            payload = ev.get("payload") or {}
            if not isinstance(payload, dict):
                continue
            for key in ("decision", "fact", "open_loop"):
                if key not in payload:
                    continue
                val = payload[key]
                items = val if isinstance(val, list) else [val]
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    note = self._flatten(key, item)
                    if not note:
                        continue
                    await self.insert_fact(
                        ArchivalFact(id=f"letta-{eid}-{written}", text=note, source_event_id=eid, tags=[key])
                    )
                    written += 1
        return written

    @staticmethod
    def _flatten(kind: str, item: Dict[str, Any]) -> str:
        if kind == "decision":
            topic = item.get("topic", "")
            stmt = item.get("statement", "")
            stance = item.get("stance", "adopted")
            why = item.get("rationale", "")
            if not topic or not stmt:
                return ""
            base = f"On {topic}: {stance} '{stmt}'"
            return base + (f" because {why}" if why else "")
        if kind == "fact":
            topic = item.get("topic", "")
            stmt = item.get("statement", "")
            return f"{topic}: {stmt}" if topic and stmt else ""
        if kind == "open_loop":
            intent = item.get("intent", "")
            return f"Open loop: {intent}" if intent else ""
        return ""

    async def rebuild_from_events(self) -> int:
        if self._client is not None:
            import asyncio

            # Re-derivation in HTTP mode means a fresh agent: drop the old one so
            # archival passages start clean, then re-create on first insert.
            await asyncio.to_thread(self._client.reset)
            self._http_agent_ready = False
            await self._ensure_http_agent()
        else:
            await self._ensure()
            await self._db._execute("DELETE FROM letta_archival")
            await self._db._execute("DELETE FROM letta_blocks")
        rows = await self._db.recent_events(limit=1_000_000)
        events: List[Dict[str, Any]] = []
        for row in rows:
            raw = row.get("payload_json")
            try:
                payload = json.loads(raw) if isinstance(raw, str) else (raw or {})
            except (TypeError, ValueError):
                payload = {}
            events.append({"id": row.get("id"), "type": row.get("type"), "payload": payload})
        return await self.consume_events(list(reversed(events)))
