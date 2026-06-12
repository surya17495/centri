"""CENTRI SQLite database — one file, one schema."""

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from centri.config import get_settings
from centri.redaction import redact_jsonable


# Tenancy key (Phase A, Decision 9). Every spine row carries a tenant_id so that
# hosted multi-tenant mode is a no-migration switch later. Single-tenant code
# paths use this default and may otherwise ignore the column; enforcement on
# query paths is Phase 6, not now.
DEFAULT_TENANT = "local"


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
"""


class Database:
    """Thread-safe async SQLite wrapper."""

    def __init__(self, path: Optional[Path] = None):
        self._path = path or get_settings().db_path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._conn: Optional[sqlite3.Connection] = None
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        conn = sqlite3.connect(str(self._path))
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

    async def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self._path))
            self._conn.row_factory = sqlite3.Row
        return self._conn

    async def _execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        async with self._lock:
            conn = await self._get_conn()
            cur = conn.execute(sql, params)
            conn.commit()
            return cur

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
        importance: str = "low",
        payload: Optional[Dict[str, Any]] = None,
        tenant_id: str = DEFAULT_TENANT,
    ) -> None:
        # Redact secrets before the payload ever touches the append-only ledger.
        payload_json = json.dumps(redact_jsonable(payload or {}))
        await self._execute(
            "INSERT INTO events (id, type, source, ts, thread_id, task_id, repo_id, tenant_id, importance, payload_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (event_id, type, source, ts, thread_id, task_id, repo_id, tenant_id, importance, payload_json),
        )

    async def recent_events(
        self,
        limit: int = 50,
        thread_id: Optional[str] = None,
        tenant_id: str = DEFAULT_TENANT,
    ) -> List[Dict[str, Any]]:
        if thread_id:
            cur = await self._execute(
                "SELECT * FROM events WHERE tenant_id = ? AND thread_id = ? ORDER BY ts DESC LIMIT ?",
                (tenant_id, thread_id, limit),
            )
        else:
            cur = await self._execute(
                "SELECT * FROM events WHERE tenant_id = ? ORDER BY ts DESC LIMIT ?",
                (tenant_id, limit),
            )
        return [dict(row) for row in cur.fetchall()]

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
            cur = await self._execute("SELECT * FROM threads WHERE status = ? ORDER BY updated_at DESC", (status,))
        else:
            cur = await self._execute("SELECT * FROM threads ORDER BY updated_at DESC")
        return [dict(row) for row in cur.fetchall()]

    async def get_thread(self, thread_id: str) -> Optional[Dict[str, Any]]:
        cur = await self._execute("SELECT * FROM threads WHERE id = ?", (thread_id,))
        row = cur.fetchone()
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
            cur = await self._execute(
                "SELECT * FROM tasks WHERE thread_id = ? AND status = ? ORDER BY updated_at DESC",
                (thread_id, status),
            )
        elif status:
            cur = await self._execute(
                "SELECT * FROM tasks WHERE status = ? ORDER BY updated_at DESC",
                (status,),
            )
        elif thread_id:
            cur = await self._execute(
                "SELECT * FROM tasks WHERE thread_id = ? ORDER BY updated_at DESC",
                (thread_id,),
            )
        else:
            cur = await self._execute("SELECT * FROM tasks ORDER BY updated_at DESC")
        return [dict(row) for row in cur.fetchall()]

    async def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        cur = await self._execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
        row = cur.fetchone()
        return dict(row) if row else None

    async def event_exists(self, event_id: str) -> bool:
        cur = await self._execute("SELECT 1 FROM events WHERE id = ? LIMIT 1", (event_id,))
        return cur.fetchone() is not None

    # ingest_state — per-source high-water mark for external store tailing (3b.3)
    async def get_ingest_state(self, source: str) -> Optional[Dict[str, Any]]:
        cur = await self._execute("SELECT * FROM ingest_state WHERE source = ?", (source,))
        row = cur.fetchone()
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
        cur = await self._execute("SELECT 1 FROM ingest_state LIMIT 1")
        return cur.fetchone() is not None

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
        async with self._lock:
            if self._conn:
                self._conn.close()
                self._conn = None

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
        cur = await self._execute("SELECT * FROM approvals WHERE id = ?", (approval_id,))
        row = cur.fetchone()
        return dict(row) if row else None

    async def pending_approvals(self, task_id: Optional[str] = None) -> List[Dict[str, Any]]:
        if task_id:
            cur = await self._execute(
                "SELECT * FROM approvals WHERE task_id = ? AND status = 'pending' ORDER BY requested_at DESC",
                (task_id,),
            )
        else:
            cur = await self._execute("SELECT * FROM approvals WHERE status = 'pending' ORDER BY requested_at DESC")
        return [dict(row) for row in cur.fetchall()]

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
            cur = await self._execute("SELECT * FROM repos WHERE root = ?", (root,))
        else:
            cur = await self._execute("SELECT * FROM repos ORDER BY last_seen DESC LIMIT 1")
        row = cur.fetchone()
        return dict(row) if row else None

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
            cur = await self._execute(
                "SELECT * FROM sessions WHERE hand = ? ORDER BY last_seen DESC LIMIT ?",
                (hand, limit),
            )
        else:
            cur = await self._execute("SELECT * FROM sessions ORDER BY last_seen DESC LIMIT ?", (limit,))
        return [dict(row) for row in cur.fetchall()]

    async def latest_session(self, hand: str = "opencode") -> Optional[Dict[str, Any]]:
        cur = await self._execute(
            "SELECT * FROM sessions WHERE hand = ? ORDER BY last_seen DESC LIMIT 1",
            (hand,),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # identity_cache
    # ------------------------------------------------------------------
    async def get_identity_cache(self, agent_id: str) -> Optional[Dict[str, Any]]:
        cur = await self._execute("SELECT * FROM identity_cache WHERE agent_id = ?", (agent_id,))
        row = cur.fetchone()
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
        cur = await self._execute("SELECT value FROM runtime_settings WHERE key = ?", (key,))
        row = cur.fetchone()
        return row["value"] if row else None

    async def set_setting_override(self, key: str, value: str) -> None:
        await self._execute(
            "INSERT INTO runtime_settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    async def get_all_setting_overrides(self) -> Dict[str, str]:
        cur = await self._execute("SELECT key, value FROM runtime_settings")
        return {row["key"]: row["value"] for row in cur.fetchall()}
