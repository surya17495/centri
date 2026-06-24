"""CENTRI typed memory graph — the semantic + prospective index on SQLite.

Design principle (CENTRI-wide): **events are the source of truth; memory is a
derived, re-derivable index.** Nothing here is authoritative. Every node carries
a *receipt* (``source_event_id``) back into the episodic ledger, and the whole
graph can be discarded and rebuilt by replaying events (see
:meth:`MemoryGraph.clear` + the consolidation worker's rebuild path).

This module implements the storage decision from ``docs/memory-architecture.md``:
a **graph schema on SQLite, no graph DB**. A single founder's builder graph is
small (~1e5 nodes after a year); recursive lookups over it are sub-millisecond.

Three node kinds, mapped to the four-memory decomposition:

  - :class:`Decision` — semantic. A choice that was made, including *rejections*
    ("we will NOT use X"). Rejections are first-class so the re-proposal guard
    (centri-bench task 1) can fire.
  - :class:`Fact` — semantic. A durable claim about the project (a convention, a
    service name, a module layout). Supersession answers "true now vs true in
    March" (task 3).
  - :class:`OpenLoop` — prospective. Something the user still intends to do, with
    a state machine (``open`` -> ``done`` / ``parked`` / ``dormant``). Surfaced
    unprompted in briefings (tasks 4, 6).

**Supersession, not accumulation.** When new truth arrives, it does not sit
beside the old — it *invalidates* it. The superseded row keeps a
``superseded_by`` pointer and an ``invalidated_at`` stamp; the live view returns
only current rows. The ledger retains everything; the index reflects only now.
This is Graphiti-style bi-temporal invalidation applied to builder state.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from centri.db import DEFAULT_TENANT

# Node kinds. ``decision`` covers both adoptions and rejections; ``stance``
# distinguishes them so the re-proposal guard can find rejected approaches.
KIND_DECISION = "decision"
KIND_FACT = "fact"
KIND_OPEN_LOOP = "open_loop"

# Reserved internal fact topics. These are derived infrastructure (e.g. the
# 3c.0 ambient standing-context digest) stored in the graph for re-derivability,
# but excluded from the general ``current_facts`` view so they never surface as
# ordinary conventions/facts. The bench, briefing, and cued curation all read the
# general view, so none of them should see these.
RESERVED_FACT_TOPICS = ("ambient-standing-context",)

# Decision stances.
STANCE_ADOPTED = "adopted"
STANCE_REJECTED = "rejected"

# Open-loop states.
LOOP_OPEN = "open"
LOOP_DONE = "done"
LOOP_PARKED = "parked"
LOOP_DORMANT = "dormant"

# When an outcome cannot be attributed to an event we record this sentinel
# rather than inventing a result (the "never confabulate" rule).
OUTCOME_UNKNOWN = "outcome unknown"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Decision:
    """A choice made about the project. Rejections are decisions too."""

    id: str
    topic: str  # normalized subject, e.g. "funding-rate signal smoothing"
    statement: str  # human-readable: "use EWMA over a 20-period SMA"
    stance: str = STANCE_ADOPTED  # adopted | rejected
    rationale: str = ""
    source_event_id: Optional[str] = None
    repo_id: Optional[str] = None
    tenant_id: str = DEFAULT_TENANT
    created_at: str = field(default_factory=_now)
    superseded_by: Optional[str] = None
    invalidated_at: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    # 3c.1 write-time embedding. Computed when the node is written if an embedding
    # provider is configured; ``None`` (default / honest-unavailable) leaves the
    # read-time cosine feature at 0.0. Stored as a JSON float array.
    vector: Optional[List[float]] = None


@dataclass
class Fact:
    """A durable claim about the project — a convention, a name, a layout."""

    id: str
    topic: str
    statement: str
    source_event_id: Optional[str] = None
    repo_id: Optional[str] = None
    tenant_id: str = DEFAULT_TENANT
    created_at: str = field(default_factory=_now)
    superseded_by: Optional[str] = None
    invalidated_at: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    # 3c.1 write-time embedding (see :class:`Decision`).
    vector: Optional[List[float]] = None


@dataclass
class OpenLoop:
    """Something the user still intends to do — prospective memory."""

    id: str
    intent: str
    state: str = LOOP_OPEN  # open | done | parked | dormant
    source_event_id: Optional[str] = None
    repo_id: Optional[str] = None
    tenant_id: str = DEFAULT_TENANT
    cue: str = ""  # free-text cue that should resurface this loop
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    last_touched_at: str = field(default_factory=_now)
    dormancy_asked_at: Optional[str] = None  # set once the yes/no line is shown
    tags: List[str] = field(default_factory=list)


class MemoryGraph:
    """SQLite-backed typed graph: decisions, facts, open loops, supersession.

    Backed by the shared :class:`centri.db.Database` connection. Tables are
    created on first use so the graph layers cleanly onto the Phase 0 schema
    without a migration step.
    """

    def __init__(self, db: Any):
        self._db = db
        self._ready = False

    async def ensure_tables(self) -> None:
        if self._ready:
            return
        # decisions + facts share the same shape; kept as two tables for clarity
        # of intent at the call site and cheap kind-scoped queries.
        await self._db._execute(
            """CREATE TABLE IF NOT EXISTS mem_decisions (
                id TEXT PRIMARY KEY,
                topic TEXT NOT NULL,
                statement TEXT NOT NULL,
                stance TEXT NOT NULL DEFAULT 'adopted',
                rationale TEXT NOT NULL DEFAULT '',
                source_event_id TEXT,
                repo_id TEXT,
                tenant_id TEXT NOT NULL DEFAULT 'local',
                created_at TEXT NOT NULL,
                superseded_by TEXT,
                invalidated_at TEXT,
                tags TEXT NOT NULL DEFAULT '',
                vector TEXT
            )"""
        )
        await self._db._execute(
            """CREATE TABLE IF NOT EXISTS mem_facts (
                id TEXT PRIMARY KEY,
                topic TEXT NOT NULL,
                statement TEXT NOT NULL,
                source_event_id TEXT,
                repo_id TEXT,
                tenant_id TEXT NOT NULL DEFAULT 'local',
                created_at TEXT NOT NULL,
                superseded_by TEXT,
                invalidated_at TEXT,
                tags TEXT NOT NULL DEFAULT '',
                vector TEXT
            )"""
        )
        await self._db._execute(
            """CREATE TABLE IF NOT EXISTS mem_open_loops (
                id TEXT PRIMARY KEY,
                intent TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'open',
                source_event_id TEXT,
                repo_id TEXT,
                tenant_id TEXT NOT NULL DEFAULT 'local',
                cue TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_touched_at TEXT NOT NULL,
                dormancy_asked_at TEXT,
                tags TEXT NOT NULL DEFAULT ''
            )"""
        )
        await self._db._execute(
            """CREATE TABLE IF NOT EXISTS mem_profile (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TEXT,
                source_event_id TEXT
            )"""
        )
        # Additive migration for graph tables created before Phase A (Decision 9):
        # ALTER in tenant_id if an older table is missing it. Mirrors Database._migrate
        # so the column is present no matter which path created the table first.
        for table in ("mem_decisions", "mem_facts", "mem_open_loops"):
            rows = await self._db._execute(f"PRAGMA table_info({table})")
            cols = {r["name"] for r in rows}
            if "tenant_id" not in cols:
                await self._db._execute(
                    f"ALTER TABLE {table} ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'local'"
                )
        # 3c.1: write-time embedding column on the semantic tables (additive, JSON
        # float array, nullable). Open loops are prospective state, not retrieval
        # candidates by vector, so they keep no vector column.
        for table in ("mem_decisions", "mem_facts"):
            rows = await self._db._execute(f"PRAGMA table_info({table})")
            cols = {r["name"] for r in rows}
            if "vector" not in cols:
                await self._db._execute(f"ALTER TABLE {table} ADD COLUMN vector TEXT")
        self._ready = True

    # ------------------------------------------------------------------
    # Write — decisions
    # ------------------------------------------------------------------
    async def add_decision(self, d: Decision) -> None:
        await self.ensure_tables()
        await self._db._execute(
            """INSERT OR REPLACE INTO mem_decisions
               (id, topic, statement, stance, rationale, source_event_id, repo_id,
                tenant_id, created_at, superseded_by, invalidated_at, tags, vector)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                d.id, d.topic, d.statement, d.stance, d.rationale, d.source_event_id,
                d.repo_id, d.tenant_id, d.created_at, d.superseded_by, d.invalidated_at,
                ",".join(d.tags), self._encode_vector(d.vector),
            ),
        )

    async def add_fact(self, f: Fact) -> None:
        await self.ensure_tables()
        await self._db._execute(
            """INSERT OR REPLACE INTO mem_facts
               (id, topic, statement, source_event_id, repo_id, tenant_id, created_at,
                superseded_by, invalidated_at, tags, vector)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                f.id, f.topic, f.statement, f.source_event_id, f.repo_id,
                f.tenant_id, f.created_at, f.superseded_by, f.invalidated_at,
                ",".join(f.tags), self._encode_vector(f.vector),
            ),
        )

    async def add_open_loop(self, loop: OpenLoop) -> None:
        await self.ensure_tables()
        await self._db._execute(
            """INSERT OR REPLACE INTO mem_open_loops
               (id, intent, state, source_event_id, repo_id, tenant_id, cue, created_at,
                updated_at, last_touched_at, dormancy_asked_at, tags)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                loop.id, loop.intent, loop.state, loop.source_event_id, loop.repo_id,
                loop.tenant_id, loop.cue, loop.created_at, loop.updated_at, loop.last_touched_at,
                loop.dormancy_asked_at, ",".join(loop.tags),
            ),
        )

    # ------------------------------------------------------------------
    # Supersession — the core mechanism. New truth invalidates old truth.
    # ------------------------------------------------------------------
    async def supersede_fact(self, new: Fact) -> Fact:
        """Insert ``new`` and invalidate any live fact on the same topic.

        Returns the inserted fact. The old fact is not deleted — it keeps a
        ``superseded_by`` pointer to ``new.id`` so "true in March" stays
        answerable, while the live view (:meth:`current_facts`) returns ``new``.
        """
        await self.ensure_tables()
        prior = await self._live_fact_for_topic(new.topic, new.repo_id)
        await self.add_fact(new)
        if prior and prior.id != new.id:
            await self._db._execute(
                "UPDATE mem_facts SET superseded_by = ?, invalidated_at = ? WHERE id = ?",
                (new.id, new.created_at, prior.id),
            )
        return new

    async def supersede_decision(self, new: Decision) -> Decision:
        """Insert ``new`` and invalidate any live decision on the same topic+stance.

        Topic+stance scoping means a later *adoption* does not silently erase an
        earlier *rejection* of a different approach on the same topic — they are
        distinct claims. A reversal is modeled as a new decision whose rationale
        cites what changed (centri-bench task 1's "states what changed" clause).
        """
        await self.ensure_tables()
        prior = await self._live_decision_for(new.topic, new.stance, new.repo_id)
        await self.add_decision(new)
        if prior and prior.id != new.id:
            await self._db._execute(
                "UPDATE mem_decisions SET superseded_by = ?, invalidated_at = ? WHERE id = ?",
                (new.id, new.created_at, prior.id),
            )
        return new

    # ------------------------------------------------------------------
    # Read — live (current) views
    # ------------------------------------------------------------------
    async def current_decisions(
        self, repo_id: Optional[str] = None, stance: Optional[str] = None
    ) -> List[Decision]:
        await self.ensure_tables()
        sql = "SELECT * FROM mem_decisions WHERE superseded_by IS NULL"
        params: List[Any] = []
        if repo_id:
            sql += " AND (repo_id = ? OR repo_id IS NULL)"
            params.append(repo_id)
        if stance:
            sql += " AND stance = ?"
            params.append(stance)
        sql += " ORDER BY created_at DESC"
        rows = await self._db._execute(sql, tuple(params))
        return [self._row_to_decision(r) for r in rows]

    async def current_facts(
        self, repo_id: Optional[str] = None, include_reserved: bool = False
    ) -> List[Fact]:
        await self.ensure_tables()
        sql = "SELECT * FROM mem_facts WHERE superseded_by IS NULL"
        params: List[Any] = []
        if repo_id:
            sql += " AND (repo_id = ? OR repo_id IS NULL)"
            params.append(repo_id)
        if not include_reserved and RESERVED_FACT_TOPICS:
            placeholders = ",".join("?" for _ in RESERVED_FACT_TOPICS)
            sql += f" AND topic NOT IN ({placeholders})"
            params.extend(RESERVED_FACT_TOPICS)
        sql += " ORDER BY created_at DESC"
        rows = await self._db._execute(sql, tuple(params))
        return [self._row_to_fact(r) for r in rows]

    async def rejected_approaches(self, repo_id: Optional[str] = None) -> List[Decision]:
        """Live rejections — what we decided NOT to do. Feeds the re-proposal guard."""
        return await self.current_decisions(repo_id=repo_id, stance=STANCE_REJECTED)

    async def fact_history(self, topic: str) -> List[Fact]:
        """All facts for a topic, newest first — for "what was true in March"."""
        await self.ensure_tables()
        rows = await self._db._execute(
            "SELECT * FROM mem_facts WHERE topic = ? ORDER BY created_at DESC", (topic,)
        )
        return [self._row_to_fact(r) for r in rows]

    async def open_loops(
        self, repo_id: Optional[str] = None, states: Optional[List[str]] = None
    ) -> List[OpenLoop]:
        await self.ensure_tables()
        states = states or [LOOP_OPEN, LOOP_DORMANT]
        placeholders = ",".join("?" for _ in states)
        sql = f"SELECT * FROM mem_open_loops WHERE LOWER(state) IN ({placeholders})"
        params: List[Any] = [s.lower() for s in states]
        if repo_id:
            sql += " AND (repo_id = ? OR repo_id IS NULL)"
            params.append(repo_id)
        sql += " ORDER BY last_touched_at ASC"
        rows = await self._db._execute(sql, tuple(params))
        return [self._row_to_loop(r) for r in rows]

    async def get_open_loop(self, loop_id: str) -> Optional[OpenLoop]:
        await self.ensure_tables()
        rows = await self._db._execute("SELECT * FROM mem_open_loops WHERE id = ?", (loop_id,))
        row = rows[0] if rows else None
        return self._row_to_loop(row) if row else None

    # ------------------------------------------------------------------
    # Open-loop state transitions
    # ------------------------------------------------------------------
    async def touch_loop(self, loop_id: str, when: Optional[str] = None) -> None:
        await self.ensure_tables()
        when = when or _now()
        await self._db._execute(
            "UPDATE mem_open_loops SET last_touched_at = ?, updated_at = ?, state = "
            "CASE WHEN LOWER(state) = 'dormant' THEN 'open' ELSE state END WHERE id = ?",
            (when, when, loop_id),
        )

    async def set_loop_state(self, loop_id: str, state: str, when: Optional[str] = None) -> None:
        await self.ensure_tables()
        when = when or _now()
        await self._db._execute(
            "UPDATE mem_open_loops SET state = ?, updated_at = ? WHERE id = ?",
            (state, when, loop_id),
        )

    async def mark_dormancy_asked(self, loop_id: str, when: Optional[str] = None) -> None:
        await self.ensure_tables()
        when = when or _now()
        await self._db._execute(
            "UPDATE mem_open_loops SET dormancy_asked_at = ?, state = ?, updated_at = ? WHERE id = ?",
            (when, LOOP_DORMANT, when, loop_id),
        )

    async def find_open_loop_by_intent(
        self, intent: str, repo_id: Optional[str] = None
    ) -> Optional[OpenLoop]:
        """Coarse de-dupe: match a loop whose intent shares the leading text."""
        await self.ensure_tables()
        key = intent.strip().lower()[:60]
        if not key:
            return None
        rows = await self._db._execute(
            "SELECT * FROM mem_open_loops WHERE LOWER(intent) LIKE ? ORDER BY created_at DESC LIMIT 1",
            (f"{key}%",),
        )
        row = rows[0] if rows else None
        return self._row_to_loop(row) if row else None

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------
    async def clear(self) -> None:
        """Discard all derived graph state. Used before a full rebuild."""
        await self.ensure_tables()
        await self._db._execute("DELETE FROM mem_decisions")
        await self._db._execute("DELETE FROM mem_facts")
        await self._db._execute("DELETE FROM mem_open_loops")

    async def reconcile_legacy_loops(self) -> int:
        """Mark still-open legacy-provenance loops as ``done``.

        Non-destructive: the rows stay in the graph (audit history preserved),
        only the state transitions from ``open``/``dormant`` to ``done``.
        Legacy loops are identified by the ``hermes``/``hal``/``mempalace`` tag
        set the ingestors stamp on them. Returns the count of loops closed.
        """
        await self.ensure_tables()
        legacy_tags = ("hermes", "hal", "mempalace")
        closed = 0
        for loop in await self.open_loops(states=[LOOP_OPEN, LOOP_DORMANT]):
            tags_lower = {t.lower() for t in loop.tags}
            if not (set(legacy_tags) & tags_lower):
                continue
            await self.set_loop_state(loop.id, LOOP_DONE)
            closed += 1
        return closed

    async def counts(self) -> Dict[str, int]:
        await self.ensure_tables()
        out: Dict[str, int] = {}
        for label, table in (
            ("decisions", "mem_decisions"),
            ("facts", "mem_facts"),
            ("open_loops", "mem_open_loops"),
        ):
            rows = await self._db._execute(f"SELECT COUNT(*) AS n FROM {table}")
            out[label] = rows[0]["n"] if rows else 0
        return out

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    async def _live_fact_for_topic(self, topic: str, repo_id: Optional[str]) -> Optional[Fact]:
        rows = await self._db._execute(
            "SELECT * FROM mem_facts WHERE topic = ? AND superseded_by IS NULL "
            "ORDER BY created_at DESC LIMIT 1",
            (topic,),
        )
        row = rows[0] if rows else None
        return self._row_to_fact(row) if row else None

    async def _live_decision_for(
        self, topic: str, stance: str, repo_id: Optional[str]
    ) -> Optional[Decision]:
        rows = await self._db._execute(
            "SELECT * FROM mem_decisions WHERE topic = ? AND stance = ? AND superseded_by IS NULL "
            "ORDER BY created_at DESC LIMIT 1",
            (topic, stance),
        )
        row = rows[0] if rows else None
        return self._row_to_decision(row) if row else None

    @staticmethod
    def _split_tags(raw: Any) -> List[str]:
        return [t for t in (raw or "").split(",") if t]

    @staticmethod
    def _encode_vector(vec: Optional[List[float]]) -> Optional[str]:
        return json.dumps(vec) if vec else None

    @staticmethod
    def _vector_of(r: Any) -> Optional[List[float]]:
        # Tolerate rows from a DB whose vector migration hasn't applied yet.
        try:
            raw = r["vector"]
        except (KeyError, IndexError):
            return None
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return None
        return [float(x) for x in data] if isinstance(data, list) else None

    @staticmethod
    def _tenant_of(r: Any) -> str:
        # Tolerate rows from a DB whose migration hasn't been applied yet (e.g. a
        # read against a legacy table mid-upgrade): default to the single-tenant key.
        try:
            return r["tenant_id"] or DEFAULT_TENANT
        except (KeyError, IndexError):
            return DEFAULT_TENANT

    def _row_to_decision(self, r: Any) -> Decision:
        return Decision(
            id=r["id"], topic=r["topic"], statement=r["statement"], stance=r["stance"],
            rationale=r["rationale"], source_event_id=r["source_event_id"],
            repo_id=r["repo_id"], tenant_id=self._tenant_of(r), created_at=r["created_at"],
            superseded_by=r["superseded_by"], invalidated_at=r["invalidated_at"],
            tags=self._split_tags(r["tags"]), vector=self._vector_of(r),
        )

    def _row_to_fact(self, r: Any) -> Fact:
        return Fact(
            id=r["id"], topic=r["topic"], statement=r["statement"],
            source_event_id=r["source_event_id"], repo_id=r["repo_id"],
            tenant_id=self._tenant_of(r), created_at=r["created_at"],
            superseded_by=r["superseded_by"],
            invalidated_at=r["invalidated_at"], tags=self._split_tags(r["tags"]),
            vector=self._vector_of(r),
        )

    def _row_to_loop(self, r: Any) -> OpenLoop:
        return OpenLoop(
            id=r["id"], intent=r["intent"], state=r["state"],
            source_event_id=r["source_event_id"], repo_id=r["repo_id"],
            tenant_id=self._tenant_of(r), cue=r["cue"],
            created_at=r["created_at"], updated_at=r["updated_at"],
            last_touched_at=r["last_touched_at"], dormancy_asked_at=r["dormancy_asked_at"],
            tags=self._split_tags(r["tags"]),
        )

    async def get_profile(self) -> Dict[str, str]:
        await self.ensure_tables()
        rows = await self._db._execute("SELECT key, value FROM mem_profile")
        return {row["key"]: row["value"] for row in rows}

    async def set_profile(self, key: str, value: str, source_event_id: str = "") -> None:
        await self.ensure_tables()
        await self._db._execute(
            """INSERT INTO mem_profile (key, value, updated_at, source_event_id)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET
                 value=excluded.value,
                 updated_at=excluded.updated_at,
                 source_event_id=excluded.source_event_id""",
            (key, value, _now(), source_event_id),
        )
