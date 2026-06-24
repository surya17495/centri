"""CENTRI SQLite database — one file, one schema."""

import asyncio
import hashlib
import json
import logging
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

from centri.config import get_settings
from centri.redaction import redact_jsonable


# Tenancy key (Phase A, Decision 9). Every spine row carries a tenant_id so that
# hosted multi-tenant mode is a no-migration switch later. Single-tenant code
# paths use this default and may otherwise ignore the column; enforcement on
# query paths is Phase 6, not now.
DEFAULT_TENANT = "local"


def _extract_fts_text(payload_json: Any) -> Optional[str]:
    if not payload_json:
        return None
    try:
        payload = json.loads(payload_json) if isinstance(payload_json, str) else payload_json
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    for key in ("text", "content", "statement", "intent", "description", "cue", "message"):
        if key in payload and payload[key] is not None:
            return str(payload[key])
    return None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    source TEXT NOT NULL,
    ts TEXT NOT NULL,
    thread_id TEXT,
    task_id TEXT,
    repo_id TEXT,
    tenant_id TEXT NOT NULL DEFAULT 'local',
    importance TEXT DEFAULT 'low',
    payload_json TEXT NOT NULL DEFAULT '{}'
);

CREATE VIRTUAL TABLE IF NOT EXISTS event_fts USING fts5(text, type, source, content_rowid='rowid');

CREATE TRIGGER IF NOT EXISTS events_after_insert AFTER INSERT ON events
WHEN CASE WHEN json_valid(new.payload_json) THEN
    coalesce(
        json_extract(new.payload_json, '$.text'),
        json_extract(new.payload_json, '$.content'),
        json_extract(new.payload_json, '$.statement'),
        json_extract(new.payload_json, '$.intent'),
        json_extract(new.payload_json, '$.description'),
        json_extract(new.payload_json, '$.cue'),
        json_extract(new.payload_json, '$.message')
    )
ELSE NULL END IS NOT NULL
BEGIN
    INSERT INTO event_fts (rowid, text, type, source)
    VALUES (
        new.rowid,
        coalesce(
            json_extract(new.payload_json, '$.text'),
            json_extract(new.payload_json, '$.content'),
            json_extract(new.payload_json, '$.statement'),
            json_extract(new.payload_json, '$.intent'),
            json_extract(new.payload_json, '$.description'),
            json_extract(new.payload_json, '$.cue'),
            json_extract(new.payload_json, '$.message')
        ),
        new.type,
        new.source
    );
END;

CREATE TRIGGER IF NOT EXISTS events_after_update AFTER UPDATE ON events
BEGIN
    DELETE FROM event_fts WHERE rowid = old.rowid;
    INSERT INTO event_fts (rowid, text, type, source)
    SELECT
        new.rowid,
        coalesce(
            json_extract(new.payload_json, '$.text'),
            json_extract(new.payload_json, '$.content'),
            json_extract(new.payload_json, '$.statement'),
            json_extract(new.payload_json, '$.intent'),
            json_extract(new.payload_json, '$.description'),
            json_extract(new.payload_json, '$.cue'),
            json_extract(new.payload_json, '$.message')
        ),
        new.type,
        new.source
    WHERE CASE WHEN json_valid(new.payload_json) THEN
        coalesce(
            json_extract(new.payload_json, '$.text'),
            json_extract(new.payload_json, '$.content'),
            json_extract(new.payload_json, '$.statement'),
            json_extract(new.payload_json, '$.intent'),
            json_extract(new.payload_json, '$.description'),
            json_extract(new.payload_json, '$.cue'),
            json_extract(new.payload_json, '$.message')
        )
    ELSE NULL END IS NOT NULL;
END;

CREATE TRIGGER IF NOT EXISTS events_after_delete AFTER DELETE ON events
BEGIN
    DELETE FROM event_fts WHERE rowid = old.rowid;
END;

CREATE TABLE IF NOT EXISTS threads (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    goal TEXT DEFAULT '',
    status TEXT DEFAULT 'active',
    repo_id TEXT,
    summary TEXT,
    next_step TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    thread_id TEXT,
    status TEXT NOT NULL,
    description TEXT NOT NULL,
    hand TEXT,
    capability TEXT,
    result TEXT,
    error TEXT,
    session_uid TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    task_id TEXT,
    thread_id TEXT,
    label TEXT NOT NULL,
    detail TEXT DEFAULT '',
    risk TEXT NOT NULL,
    artifact_json TEXT DEFAULT '{}',
    requested_action TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    requested_at TEXT NOT NULL,
    responded_at TEXT,
    responded_by TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    channel TEXT NOT NULL,
    user_id TEXT NOT NULL,
    direction TEXT NOT NULL,
    content TEXT NOT NULL,
    is_voice INTEGER DEFAULT 0,
    transcript TEXT,
    ts TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'topic',
    ref TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS projects_kind_idx ON projects (kind);
CREATE INDEX IF NOT EXISTS projects_ref_idx ON projects (ref);

-- The `repo_id` columns on events/threads/sessions are project_id references
-- (kept under the legacy name to avoid a 100+ site rename). A coding repo is a
-- project of kind='repo'; research, conversations, voice threads are projects
-- of other kinds. The repos table retains coding-specific metadata (branch,
-- ahead/behind) for kind='repo' projects only.
CREATE TABLE IF NOT EXISTS repos (
    id TEXT PRIMARY KEY,
    root TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    branch TEXT,
    dirty INTEGER DEFAULT 0,
    ahead INTEGER DEFAULT 0,
    behind INTEGER DEFAULT 0,
    last_seen TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    session_uid TEXT UNIQUE,
    hand TEXT NOT NULL,
    status TEXT NOT NULL,
    repo_id TEXT,
    summary TEXT,
    last_seen TEXT NOT NULL,
    payload_json TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS identity_cache (
    agent_id TEXT PRIMARY KEY,
    blocks_json TEXT DEFAULT '{}',
    persona TEXT DEFAULT '',
    human TEXT DEFAULT '',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingest_state (
    source TEXT PRIMARY KEY,
    high_water TEXT NOT NULL DEFAULT '',
    last_run_at TEXT,
    ingested_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS runtime_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS working_memory (
    thread_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (thread_id, key)
);
"""


class Database:
    """Thread-safe async SQLite wrapper."""

    def __init__(self, path: Optional[Path] = None):
        self._path = path or get_settings().db_path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        conn = sqlite3.connect(str(self._path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")  
        conn.execute("PRAGMA busy_timeout=5000")
        conn.executescript(_SCHEMA)
        self._migrate(conn)
        conn.commit()
        conn.close()

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Idempotent in-place migrations for DBs created before a schema change.

        `CREATE TABLE IF NOT EXISTS` does not alter an existing table, so a DB
        from a prior version keeps its old columns. Each migration here is an
        additive `ALTER TABLE ... ADD COLUMN` with a constant default, which
        SQLite applies without rewriting rows: existing data is preserved and
        every pre-existing row gets the default value.

        Phase A (Decision 9): add `tenant_id` to the spine and the derived graph
        tables so hosted multi-tenant mode is a no-migration switch later. The
        derived graph tables are created lazily by MemoryGraph; we only ALTER
        them here if they already exist (otherwise they are born with the column).
        """
        tenancy_targets = (
            "events",
            "mem_decisions",
            "mem_facts",
            "mem_open_loops",
        )
        existing = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        for table in tenancy_targets:
            if table not in existing:
                continue
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if "tenant_id" not in cols:
                conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'local'"
                )

        # Backfill event_fts if it is empty but events is not
        try:
            has_fts = conn.execute("SELECT 1 FROM event_fts LIMIT 1").fetchone()
            has_events = conn.execute("SELECT 1 FROM events LIMIT 1").fetchone()
            if has_events and not has_fts:
                cursor = conn.execute("SELECT rowid, type, source, payload_json FROM events")
                for rowid, type_, source, payload_json in cursor.fetchall():
                    text_to_index = _extract_fts_text(payload_json)
                    if text_to_index is not None:
                        conn.execute(
                            "INSERT OR IGNORE INTO event_fts (rowid, text, type, source) VALUES (?, ?, ?, ?)",
                            (rowid, text_to_index, type_, source),
                        )
        except sqlite3.OperationalError:
            # In case FTS5 is not available or event_fts fails, ignore to prevent breaking the boot
            pass

        # Backfill existing repos into the projects table as kind='repo' so the
        # universal project scoping picks them up. Idempotent: existing project
        # ids are preserved.
        if "repos" in existing and "projects" in existing:
            from datetime import datetime, timezone

            now_iso = datetime.now(timezone.utc).isoformat()
            for row in conn.execute(
                "SELECT id, name, root FROM repos"
            ).fetchall():
                conn.execute(
                    """INSERT OR IGNORE INTO projects (id, name, kind, ref, created_at, updated_at)
                       VALUES (?, ?, 'repo', ?, ?, ?)""",
                    (row[0], row[1], row[2], now_iso, now_iso),
                )

    def _get_thread_conn(self) -> sqlite3.Connection:
        """Get or create a thread-local connection. Each uvicorn worker thread
        gets its own connection, cached in thread-local storage. WAL mode allows
        concurrent readers; busy_timeout handles writer contention."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self._path), check_same_thread=True)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
            self._local.conn = conn
        return conn

    async def _execute(self, sql: str, params: tuple = ()) -> List[sqlite3.Row]:
        def _run():
            conn = self._get_thread_conn()
            cur = conn.execute(sql, params)
            conn.commit()
            return cur.fetchall()
        return await asyncio.to_thread(_run)

    # events
    async def append_event(
        self,
        event_id: str,
        type: str,
        source: str,
        ts: str,
        thread_id: Optional[str] = None,
        task_id: Optional[str] = None,
        repo_id: Optional[str] = None,
        importance: str = "normal",
        payload: Optional[Dict[str, Any]] = None,
        tenant_id: str = DEFAULT_TENANT,
    ) -> None:
        # Redact secrets before the payload ever touches the append-only ledger.
        payload_json = json.dumps(redact_jsonable(payload or {}))

        def _run():
            conn = self._get_thread_conn()
            conn.execute(
                "INSERT INTO events (id, type, source, ts, thread_id, task_id, repo_id, tenant_id, importance, payload_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (event_id, type, source, ts, thread_id, task_id, repo_id, tenant_id, importance, payload_json),
            )
            conn.commit()
        await asyncio.to_thread(_run)

    _IMPORTANCE_RANK = {"low": 0, "normal": 1, "high": 2}

    async def recent_events(
        self,
        limit: int = 50,
        thread_id: Optional[str] = None,
        tenant_id: str = DEFAULT_TENANT,
        min_importance: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Recent events, newest first.

        ``min_importance`` filters by importance tier when set (e.g. ``"normal"``
        excludes ``low``-importance tool output and audit noise). ``None`` (the
        default) returns everything for backward-compatible callers like anaphora
        resolution, which needs recent turns regardless of importance tier.
        """
        importance_clause = ""
        params: list = []
        if min_importance and min_importance in self._IMPORTANCE_RANK:
            threshold = self._IMPORTANCE_RANK[min_importance]
            allowed = [k for k, v in self._IMPORTANCE_RANK.items() if v >= threshold]
            placeholders = ",".join("?" for _ in allowed)
            importance_clause = f" AND importance IN ({placeholders})"
            params.extend(allowed)

        if thread_id:
            params.extend([tenant_id, thread_id, limit])
            rows = await self._execute(
                f"SELECT * FROM events WHERE tenant_id = ? AND thread_id = ?{importance_clause} ORDER BY ts DESC LIMIT ?",
                tuple(params),
            )
        else:
            params.extend([tenant_id, limit])
            rows = await self._execute(
                f"SELECT * FROM events WHERE tenant_id = ?{importance_clause} ORDER BY ts DESC LIMIT ?",
                tuple(params),
            )
        return [dict(row) for row in rows]

    async def events_after(
        self,
        after_ts: str = "",
        after_id: str = "",
        limit: int = 100,
        tenant_id: str = DEFAULT_TENANT,
        min_importance: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Return the next spine events after a timestamp, oldest first.

        Scheduler consolidation needs a bounded forward scan. Using recent_events()
        would fetch newest-first and can skip older imported rows when the backlog is
        larger than the fetch limit.

        ``min_importance`` filters by importance tier when set. The default (``None``)
        returns everything, which is what the consolidation rebuild path needs — the
        deterministic tier then applies its own importance filter during promotion.
        """
        importance_clause = ""
        params: list = []
        if min_importance and min_importance in self._IMPORTANCE_RANK:
            threshold = self._IMPORTANCE_RANK[min_importance]
            allowed = [k for k, v in self._IMPORTANCE_RANK.items() if v >= threshold]
            placeholders = ",".join("?" for _ in allowed)
            importance_clause = f" AND importance IN ({placeholders})"
            params.extend(allowed)

        if after_ts:
            params.extend([tenant_id, after_ts, after_ts, after_id, limit])
            rows = await self._execute(
                f"SELECT * FROM events WHERE tenant_id = ? AND (ts > ? OR (ts = ? AND id > ?)){importance_clause} ORDER BY ts ASC, id ASC LIMIT ?",
                tuple(params),
            )
        else:
            params.extend([tenant_id, limit])
            rows = await self._execute(
                f"SELECT * FROM events WHERE tenant_id = ?{importance_clause} ORDER BY ts ASC, id ASC LIMIT ?",
                tuple(params),
            )
        return [dict(row) for row in rows]

    async def search_events(
        self,
        query: str,
        limit: int = 10,
        min_importance: str = "normal",
    ) -> List[Dict[str, Any]]:
        """Verbatim FTS recall, filtered to durable signal.

        ``min_importance='normal'`` (the default) excludes ``low``-importance
        events (tool output, audit noise) so verbatim recall surfaces real
        transcript and decisions, not shell-command snippets. Pass
        ``min_importance='low'`` to search the full spine.

        ``consolidation.*`` event sources are always excluded: they are the
        memory system talking to itself (proposal applied/rejected, batch
        summaries) and never belong in user-facing verbatim recall.
        """
        try:
            # Importance ordering: low < normal < high. We keep everything at or
            # above the threshold by mapping to an explicit set.
            allowed = {
                "low": {"low", "normal", "high"},
                "normal": {"normal", "high"},
                "high": {"high"},
            }[min_importance]
            placeholders = ",".join("?" for _ in allowed)
            rows = await self._execute(
                f"""
                SELECT events.id AS event_id, event_fts.text, event_fts.type,
                       event_fts.source, events.thread_id
                FROM event_fts
                JOIN events ON events.rowid = event_fts.rowid
                WHERE event_fts MATCH ?
                  AND events.importance IN ({placeholders})
                  AND events.source NOT LIKE 'consolidation%'
                  AND events.source NOT LIKE 'memory%'
                ORDER BY bm25(event_fts) ASC
                LIMIT ?
                """,
                (query, *allowed, limit),
            )
            return [dict(row) for row in rows]
        except sqlite3.OperationalError as e:
            logger.warning("FTS search failed: %s", e)
            return []

    # threads
    async def create_thread(
        self,
        thread_id: str,
        title: str,
        goal: str,
        repo_id: Optional[str] = None,
        created_at: Optional[str] = None,
        updated_at: Optional[str] = None,
    ) -> None:
        await self._execute(
            "INSERT INTO threads (id, title, goal, repo_id, created_at, updated_at) VALUES (?,?,?,?,?,?)",
            (thread_id, title, goal, repo_id, created_at, updated_at),
        )

    async def list_threads(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        if status:
            rows = await self._execute("SELECT * FROM threads WHERE status = ? ORDER BY updated_at DESC", (status,))
        else:
            rows = await self._execute("SELECT * FROM threads ORDER BY updated_at DESC")
        return [dict(row) for row in rows]

    async def get_thread(self, thread_id: str) -> Optional[Dict[str, Any]]:
        rows = await self._execute("SELECT * FROM threads WHERE id = ?", (thread_id,))
        row = rows[0] if rows else None
        return dict(row) if row else None

    async def update_thread(
        self,
        thread_id: str,
        status: Optional[str] = None,
        summary: Optional[str] = None,
        next_step: Optional[str] = None,
        updated_at: Optional[str] = None,
    ) -> None:
        fields = []
        params = []
        if status:
            fields.append("status = ?")
            params.append(status)
        if summary:
            fields.append("summary = ?")
            params.append(summary)
        if next_step:
            fields.append("next_step = ?")
            params.append(next_step)
        if updated_at:
            fields.append("updated_at = ?")
            params.append(updated_at)
        if not fields:
            return
        params.append(thread_id)
        await self._execute(
            f"UPDATE threads SET {', '.join(fields)} WHERE id = ?",
            tuple(params),
        )

    # tasks
    async def create_task(
        self,
        task_id: str,
        thread_id: Optional[str],
        description: str,
        hand: Optional[str] = None,
        capability: Optional[str] = None,
        created_at: Optional[str] = None,
        updated_at: Optional[str] = None,
    ) -> None:
        await self._execute(
            "INSERT INTO tasks (id, thread_id, status, description, hand, capability, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (task_id, thread_id, "pending", description, hand, capability, created_at, updated_at),
        )

    async def update_task(
        self,
        task_id: str,
        status: Optional[str] = None,
        result: Optional[str] = None,
        error: Optional[str] = None,
        session_uid: Optional[str] = None,
        hand: Optional[str] = None,
        capability: Optional[str] = None,
        updated_at: Optional[str] = None,
        completed_at: Optional[str] = None,
    ) -> None:
        fields: List[str] = []
        params: List[Any] = []
        if status:
            fields.append("status = ?")
            params.append(status)
        if result:
            fields.append("result = ?")
            params.append(result)
        if error:
            fields.append("error = ?")
            params.append(error)
        if session_uid:
            fields.append("session_uid = ?")
            params.append(session_uid)
        if hand:
            fields.append("hand = ?")
            params.append(hand)
        if capability:
            fields.append("capability = ?")
            params.append(capability)
        if updated_at:
            fields.append("updated_at = ?")
            params.append(updated_at)
        if completed_at:
            fields.append("completed_at = ?")
            params.append(completed_at)
        if not fields:
            return
        params.append(task_id)
        await self._execute(
            f"UPDATE tasks SET {', '.join(fields)} WHERE id = ?",
            tuple(params),
        )

    async def list_tasks(self, thread_id: Optional[str] = None, status: Optional[str] = None) -> List[Dict[str, Any]]:
        if thread_id and status:
            rows = await self._execute(
                "SELECT * FROM tasks WHERE thread_id = ? AND status = ? ORDER BY updated_at DESC",
                (thread_id, status),
            )
        elif status:
            rows = await self._execute(
                "SELECT * FROM tasks WHERE status = ? ORDER BY updated_at DESC",
                (status,),
            )
        elif thread_id:
            rows = await self._execute(
                "SELECT * FROM tasks WHERE thread_id = ? ORDER BY updated_at DESC",
                (thread_id,),
            )
        else:
            rows = await self._execute("SELECT * FROM tasks ORDER BY updated_at DESC")
        return [dict(row) for row in rows]

    async def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        rows = await self._execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
        row = rows[0] if rows else None
        return dict(row) if row else None

    async def event_exists(self, event_id: str) -> bool:
        rows = await self._execute("SELECT 1 FROM events WHERE id = ? LIMIT 1", (event_id,))
        return bool(rows)

    # ingest_state — per-source high-water mark for external store tailing (3b.3)
    async def get_ingest_state(self, source: str) -> Optional[Dict[str, Any]]:
        rows = await self._execute("SELECT * FROM ingest_state WHERE source = ?", (source,))
        row = rows[0] if rows else None
        return dict(row) if row else None

    async def get_ingest_high_water(self, source: str) -> str:
        state = await self.get_ingest_state(source)
        return state["high_water"] if state else ""

    async def has_ingest_state(self) -> bool:
        """True if any source has ever been ingested (i.e. bootstrap has run).

        The first-run onboarding flag derives from this — a backend fact, not
        client localStorage — so a re-install / new client still knows whether
        memory has been seeded from the user's coding-agent histories.
        """
        rows = await self._execute("SELECT 1 FROM ingest_state LIMIT 1")
        return bool(rows)

    async def set_ingest_high_water(
        self,
        source: str,
        high_water: str,
        last_run_at: Optional[str] = None,
        ingested_delta: int = 0,
    ) -> None:
        await self._execute(
            """INSERT INTO ingest_state (source, high_water, last_run_at, ingested_count)
                VALUES (?,?,?,?)
                ON CONFLICT(source) DO UPDATE SET
                  high_water=excluded.high_water,
                  last_run_at=excluded.last_run_at,
                  ingested_count=ingest_state.ingested_count + excluded.ingested_count""",
            (source, high_water, last_run_at, ingested_delta),
        )

    # Shutdown hook
    async def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            self._local.conn = None
            await asyncio.to_thread(conn.close)

    # approvals
    async def create_approval(
        self,
        approval_id: str,
        task_id: Optional[str],
        thread_id: Optional[str],
        label: str,
        detail: str,
        risk: str,
        requested_action: str,
        requested_at: str,
        artifact: Optional[Dict[str, Any]] = None,
    ) -> None:
        artifact_json = json.dumps(artifact or {})
        await self._execute(
            "INSERT INTO approvals (id, task_id, thread_id, label, detail, risk, requested_action, requested_at, artifact_json) VALUES (?,?,?,?,?,?,?,?,?)",
            (approval_id, task_id, thread_id, label, detail, risk, requested_action, requested_at, artifact_json),
        )

    async def resolve_approval(
        self,
        approval_id: str,
        status: str,
        responded_by: str,
        responded_at: str,
    ) -> None:
        await self._execute(
            "UPDATE approvals SET status = ?, responded_by = ?, responded_at = ? WHERE id = ?",
            (status, responded_by, responded_at, approval_id),
        )

    async def get_approval(self, approval_id: str) -> Optional[Dict[str, Any]]:
        rows = await self._execute("SELECT * FROM approvals WHERE id = ?", (approval_id,))
        row = rows[0] if rows else None
        return dict(row) if row else None

    async def pending_approvals(self, task_id: Optional[str] = None) -> List[Dict[str, Any]]:
        if task_id:
            rows = await self._execute(
                "SELECT * FROM approvals WHERE task_id = ? AND status = 'pending' ORDER BY requested_at DESC",
                (task_id,),
            )
        else:
            rows = await self._execute("SELECT * FROM approvals WHERE status = 'pending' ORDER BY requested_at DESC")
        return [dict(row) for row in rows]

    # messages
    async def store_message(
        self,
        channel: str,
        user_id: str,
        direction: str,
        content: str,
        ts: str,
        is_voice: bool = False,
        transcript: Optional[str] = None,
    ) -> None:
        await self._execute(
            "INSERT INTO messages (id, channel, user_id, direction, content, is_voice, transcript, ts) VALUES (?,?,?,?,?,?,?,?)",
            (f"msg-{ts}-{channel}", channel, user_id, direction, content, int(is_voice), transcript, ts),
        )

    # repos
    async def upsert_repo(
        self,
        repo_id: str,
        root: str,
        name: str,
        branch: Optional[str] = None,
        dirty: bool = False,
        ahead: int = 0,
        behind: int = 0,
        last_seen: Optional[str] = None,
    ) -> None:
        await self._execute(
            """INSERT INTO repos (id, root, name, branch, dirty, ahead, behind, last_seen) VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET branch=excluded.branch, dirty=excluded.dirty, ahead=excluded.ahead, behind=excluded.behind, last_seen=excluded.last_seen""",
            (repo_id, root, name, branch, int(dirty), ahead, behind, last_seen),
        )

    async def active_repo(self, root: Optional[str] = None) -> Optional[Dict[str, Any]]:
        if root:
            rows = await self._execute("SELECT * FROM repos WHERE root = ?", (root,))
        else:
            rows = await self._execute("SELECT * FROM repos ORDER BY last_seen DESC LIMIT 1")
        row = rows[0] if rows else None
        return dict(row) if row else None

    # projects
    async def upsert_project(
        self,
        project_id: str,
        name: str,
        kind: str = "topic",
        ref: Optional[str] = None,
        created_at: Optional[str] = None,
        updated_at: Optional[str] = None,
    ) -> str:
        ts = created_at or _now_iso()
        upd = updated_at or ts
        await self._execute(
            """INSERT INTO projects (id, name, kind, ref, created_at, updated_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET name=excluded.name, kind=excluded.kind, ref=excluded.ref, updated_at=excluded.updated_at""",
            (project_id, name, kind, ref, ts, upd),
        )
        return project_id

    async def get_project(self, project_id: str) -> Optional[Dict[str, Any]]:
        rows = await self._execute("SELECT * FROM projects WHERE id = ?", (project_id,))
        row = rows[0] if rows else None
        return dict(row) if row else None

    async def get_project_by_ref(self, kind: str, ref: str) -> Optional[Dict[str, Any]]:
        rows = await self._execute(
            "SELECT * FROM projects WHERE kind = ? AND ref = ? LIMIT 1",
            (kind, ref),
        )
        row = rows[0] if rows else None
        return dict(row) if row else None

    async def resolve_project_for_repo(
        self, root: str, name: Optional[str] = None, branch: Optional[str] = None
    ) -> str:
        """Resolve a repo path to a project id, creating both rows if absent.

        Coding repos are double-bookkept: a `repos` row holds the coding-specific
        metadata (branch, ahead/behind), and a `projects` row of kind='repo' is
        the universal scoping reference. The project's `ref` is the repo root so
        a single lookup by path resolves the project.
        """
        existing = await self.get_project_by_ref("repo", root)
        if existing:
            return existing["id"]
        repo_name = name or Path(root).name or "repo"
        project_id = f"repo:{repo_name}:{hashlib.sha1(root.encode()).hexdigest()[:8]}"
        await self.upsert_project(project_id, repo_name, kind="repo", ref=root)
        await self.upsert_repo(
            repo_id=project_id,
            root=root,
            name=repo_name,
            branch=branch,
            last_seen=_now_iso(),
        )
        return project_id

    async def resolve_project_for_topic(self, topic: str, name: Optional[str] = None) -> str:
        """Resolve an arbitrary topic/slug to a project id, creating it if absent.

        Used by non-coding ingest paths (OpenCode sessions without a directory,
        voice conversations, research threads). The `ref` is the topic slug so
        repeated ingest of the same topic resolves to the same project.
        """
        slug = re.sub(r"[^a-z0-9]+", "-", (topic or "").lower()).strip("-") or "untitled"
        existing = await self.get_project_by_ref("topic", slug)
        if existing:
            return existing["id"]
        project_id = f"topic:{slug}:{hashlib.sha1(slug.encode()).hexdigest()[:8]}"
        await self.upsert_project(project_id, name or slug, kind="topic", ref=slug)
        return project_id

    async def backfill_project_ids(self) -> Dict[str, int]:
        """Backfill ``repo_id`` (project_id) on events that lack one.

        Events ingested before project-keyed scoping was added have an empty
        ``repo_id``. This method resolves a project for each by looking at the
        event's ``thread_id`` (session) — if the session's payload or the event
        payload carries a ``directory`` field, that resolves to a repo project;
        otherwise it falls back to a topic project from the session title or
        the event payload. Returns a count dict: ``{resolved, skipped}``.
        """
        import json as _json

        rows = await self._execute(
            "SELECT id, thread_id, source, payload_json FROM events WHERE repo_id IS NULL OR repo_id = ''"
        )
        resolved = 0
        skipped = 0

        # Build a thread_id → project_id cache so each session is resolved once.
        thread_cache: Dict[str, Optional[str]] = {}

        for row in rows:
            thread_id = row["thread_id"] or ""

            # Try the thread cache first.
            if thread_id and thread_id in thread_cache:
                project_id = thread_cache[thread_id]
            else:
                project_id = None

                # Look up the session row for directory/title metadata.
                if thread_id:
                    sresults = await self._execute(
                        "SELECT payload_json FROM sessions WHERE id = ?", (thread_id,)
                    )
                    sresult = sresults[0] if sresults else None
                    if sresult:
                        try:
                            spayload = _json.loads(sresult["payload_json"]) if sresult["payload_json"] else {}
                        except (TypeError, ValueError):
                            spayload = {}
                        directory = (spayload.get("directory") or "").strip()
                        if directory:
                            project_id = await self.resolve_project_for_repo(directory)
                        else:
                            title = (spayload.get("title") or "").strip()
                            if title:
                                project_id = await self.resolve_project_for_topic(title)

                # Fall back to the event payload itself.
                if not project_id:
                    try:
                        payload = _json.loads(row["payload_json"]) if isinstance(row["payload_json"], str) else (row["payload_json"] or {})
                    except (TypeError, ValueError):
                        payload = {}
                    if isinstance(payload, dict):
                        directory = (payload.get("directory") or "").strip()
                        if directory:
                            project_id = await self.resolve_project_for_repo(directory)
                        else:
                            slug = (payload.get("slug") or "").strip()
                            title = (payload.get("title") or "").strip()
                            if slug:
                                project_id = await self.resolve_project_for_topic(slug)
                            elif title:
                                project_id = await self.resolve_project_for_topic(title)

                if thread_id:
                    thread_cache[thread_id] = project_id

            if project_id:
                await self._execute(
                    "UPDATE events SET repo_id = ? WHERE id = ?",
                    (project_id, row["id"]),
                )
                resolved += 1
            else:
                skipped += 1

        return {"resolved": resolved, "skipped": skipped}

    # working memory — the "what am I doing right now" store
    async def set_working_context(self, thread_id: str, key: str, value: str) -> None:
        """Store a working-memory entry for the active thread.

        Working memory holds short-lived context that bridges turns within a
        session — the current task, unresolved sub-questions, active files —
        so mid-session continuity doesn't depend on re-deriving state from
        the full spine. Entries are keyed by ``(thread_id, key)`` and
        upserted, so re-setting the same key updates in place.
        """
        await self._execute(
            "INSERT INTO working_memory (thread_id, key, value, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(thread_id, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (thread_id, key, value, _now_iso()),
        )

    async def get_working_context(self, thread_id: str) -> Dict[str, str]:
        """Read all working-memory entries for a thread."""
        rows = await self._execute(
            "SELECT key, value FROM working_memory WHERE thread_id = ?",
            (thread_id,),
        )
        return {row["key"]: row["value"] for row in rows}

    async def clear_working_context(self, thread_id: str) -> None:
        """Discard all working-memory entries for a thread (on session end)."""
        await self._execute(
            "DELETE FROM working_memory WHERE thread_id = ?",
            (thread_id,),
        )

    # sessions
    async def upsert_session(
        self,
        session_id: str,
        session_uid: Optional[str],
        hand: str,
        status: str,
        repo_id: Optional[str] = None,
        summary: Optional[str] = None,
        last_seen: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        payload_json = json.dumps(redact_jsonable(payload or {}))
        await self._execute(
            """INSERT INTO sessions (id, session_uid, hand, status, repo_id, summary, last_seen, payload_json) VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET session_uid=excluded.session_uid, status=excluded.status, repo_id=excluded.repo_id, summary=excluded.summary, last_seen=excluded.last_seen, payload_json=excluded.payload_json""",
            (session_id, session_uid, hand, status, repo_id, summary, last_seen, payload_json),
        )

    async def list_sessions(self, hand: Optional[str] = None, limit: int = 20) -> List[Dict[str, Any]]:
        if hand:
            rows = await self._execute(
                "SELECT * FROM sessions WHERE hand = ? ORDER BY last_seen DESC LIMIT ?",
                (hand, limit),
            )
        else:
            rows = await self._execute("SELECT * FROM sessions ORDER BY last_seen DESC LIMIT ?", (limit,))
        return [dict(row) for row in rows]

    async def latest_session(self, hand: str = "opencode") -> Optional[Dict[str, Any]]:
        rows = await self._execute(
            "SELECT * FROM sessions WHERE hand = ? ORDER BY last_seen DESC LIMIT 1",
            (hand,),
        )
        row = rows[0] if rows else None
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # identity_cache
    # ------------------------------------------------------------------
    async def get_identity_cache(self, agent_id: str) -> Optional[Dict[str, Any]]:
        rows = await self._execute("SELECT * FROM identity_cache WHERE agent_id = ?", (agent_id,))
        row = rows[0] if rows else None
        return dict(row) if row else None

    async def upsert_identity_cache(
        self, agent_id: str, blocks_json: str, persona: str, human: str, updated_at: str
    ) -> None:
        await self._execute(
            """INSERT INTO identity_cache (agent_id, blocks_json, persona, human, updated_at)
                VALUES (?,?,?,?,?)
                ON CONFLICT(agent_id) DO UPDATE SET
                  blocks_json=excluded.blocks_json,
                  persona=excluded.persona,
                  human=excluded.human,
                  updated_at=excluded.updated_at""",
            (agent_id, blocks_json, persona, human, updated_at),
        )

    # runtime_settings
    async def get_setting_override(self, key: str) -> Optional[str]:
        rows = await self._execute("SELECT value FROM runtime_settings WHERE key = ?", (key,))
        row = rows[0] if rows else None
        return row["value"] if row else None

    async def set_setting_override(self, key: str, value: str) -> None:
        await self._execute(
            "INSERT INTO runtime_settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    async def get_all_setting_overrides(self) -> Dict[str, str]:
        rows = await self._execute("SELECT key, value FROM runtime_settings")
        return {row["key"]: row["value"] for row in rows}


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
