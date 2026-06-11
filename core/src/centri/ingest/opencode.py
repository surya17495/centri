"""CENTRI OpenCode ingestion adapter (ROADMAP 3b.3, registry form in 3b.4).

Tails an external ``opencode.db`` — the SQLite store OpenCode keeps for its own
sessions/messages — into ``ingest.opencode.message`` spine events. This makes
coding work a developer did *directly* in OpenCode (outside CENTRI's hand path)
visible to the memory graph: each ingested message becomes an event, and the
consolidation worker folds the per-session digest into a typed fact exactly as
it does for native ``hand.transcript`` events.

The shared HWM/idempotency/redaction/write core now lives in
:mod:`centri.ingest.base`; this module is just the OpenCode-specific reader plus
its labels. Behavior is unchanged from 3b.3.

The read path is **read-only** (the external DB is opened ``mode=ro``) and
**idempotent**: event ids are deterministic and the high-water mark is persisted
per source, so re-running an ingest never duplicates events.

OpenCode's on-disk schema has shifted across versions, so the reader is tolerant
about column names. It targets a ``message`` table with, per row:

  - a stable id              (``id`` / ``message_id`` / ``rowid``)
  - a session id             (``session_id`` / ``sessionID`` / ``session``)
  - a role                   (``role`` — user / assistant / system / tool)
  - text content             (``content`` / ``text`` / ``body``; JSON "parts"
                              arrays are flattened to their text fields)
  - a creation timestamp     (``created_at`` / ``created`` / ``time_created`` /
                              ``time`` / ``ts``)

Rows are ordered by (timestamp, id) and the high-water mark is the ``"ts|id"`` of
the last ingested row, so ordering is stable even when timestamps collide.
"""

from __future__ import annotations

import logging
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from centri.ingest.base import (
    DiscoveredSource,
    MessageAdapter,
    coerce_ts,
    first_present,
    flatten_content,
)

logger = logging.getLogger(__name__)


# Candidate column names, most-specific first. The reader picks the first one
# present on the message table.
_ID_COLS = ("id", "message_id", "messageID", "msg_id")
_SESSION_COLS = ("session_id", "sessionID", "session", "sessionId", "thread_id")
_ROLE_COLS = ("role", "type", "author", "sender")
_CONTENT_COLS = ("content", "text", "body", "message", "parts")
_TS_COLS = ("created_at", "createdAt", "created", "time_created", "time", "ts", "timestamp")
_MESSAGE_TABLES = ("message", "messages", "Message")


def _opencode_default_locations() -> List[Path]:
    """Well-known default opencode.db locations per platform (macOS/Linux)."""
    home = Path.home()
    paths: List[Path] = []
    # XDG / Linux + macOS share state dir conventions across opencode versions.
    paths.append(home / ".local" / "share" / "opencode" / "opencode.db")
    paths.append(home / ".config" / "opencode" / "opencode.db")
    paths.append(home / ".opencode" / "opencode.db")
    if sys.platform == "darwin":
        paths.append(home / "Library" / "Application Support" / "opencode" / "opencode.db")
    return paths


class OpenCodeIngestor(MessageAdapter):
    """Incremental, idempotent tail of an ``opencode.db`` into spine events."""

    agent = "opencode"
    tool = "opencode"
    event_type = "ingest.opencode.message"
    event_source = "ingest.opencode"
    source_prefix = "opencode"
    fact_tags = ("ingest", "opencode", "transcript")

    def default_locations(self) -> List[Path]:
        return _opencode_default_locations()

    def fact_topic(self, row: Dict[str, Any]) -> str:
        session_id = row.get("session_id") or row.get("id")
        return f"opencode-session:{session_id}"

    def fact_statement(self, row: Dict[str, Any]) -> str:
        session_id = row.get("session_id") or row.get("id")
        role = row.get("role") or "unknown"
        content = (row.get("content") or "").strip()
        return f"OpenCode session {session_id} ({role}): {content[:400]}"

    # ------------------------------------------------------------------
    # External DB read (read-only)
    # ------------------------------------------------------------------
    def read_messages(self, path: Path) -> List[Dict[str, Any]]:
        uri = f"file:{path.resolve()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            conn.row_factory = sqlite3.Row
            table = self._resolve_table(conn)
            if table is None:
                raise ValueError("no message table found in opencode.db")
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
            id_col = first_present(cols, _ID_COLS)
            session_col = first_present(cols, _SESSION_COLS)
            role_col = first_present(cols, _ROLE_COLS)
            content_col = first_present(cols, _CONTENT_COLS)
            ts_col = first_present(cols, _TS_COLS)

            # rowid is always available as the id fallback for tables whose
            # primary id column is absent or null.
            raw_rows = conn.execute(f"SELECT rowid AS _rowid, * FROM {table}").fetchall()
            out: List[Dict[str, Any]] = []
            for r in raw_rows:
                rd = dict(r)
                rid = rd.get(id_col) if id_col else rd.get("_rowid")
                if rid in (None, ""):
                    rid = rd.get("_rowid")
                out.append(
                    {
                        "id": str(rid),
                        "session_id": str(rd.get(session_col)) if session_col and rd.get(session_col) is not None else "",
                        "role": str(rd.get(role_col)) if role_col and rd.get(role_col) is not None else "",
                        "content": flatten_content(rd.get(content_col)) if content_col else "",
                        "ts": coerce_ts(rd.get(ts_col)) if ts_col else "",
                    }
                )
            return out
        finally:
            conn.close()

    def _resolve_table(self, conn: sqlite3.Connection) -> Optional[str]:
        names = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        for cand in _MESSAGE_TABLES:
            if cand in names:
                return cand
        # Fall back to any table that has a content-like and a role-like col.
        for name in names:
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({name})").fetchall()]
            if first_present(cols, _CONTENT_COLS) and first_present(cols, _ROLE_COLS):
                return name
        return None

    def _cheap_count(self, path: Path) -> Tuple[Optional[int], str]:
        try:
            uri = f"file:{path.resolve()}?mode=ro"
            conn = sqlite3.connect(uri, uri=True)
            try:
                table = self._resolve_table(conn)
                if table is None:
                    return None, "no message table found"
                row = conn.execute(f"SELECT count(*) FROM {table}").fetchone()
                return int(row[0]) if row else 0, ""
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001
            return None, f"unreadable: {exc}"


async def ingest_opencode_db(
    db: Any,
    opencode_db_path: str | Path,
    source: Optional[str] = None,
    repo_id: Optional[str] = None,
    event_bus: Any = None,
) -> Dict[str, Any]:
    """Convenience one-shot: build an ingestor and run a single pass."""
    return await OpenCodeIngestor(db, event_bus=event_bus).ingest(
        opencode_db_path, source=source, repo_id=repo_id
    )
