"""CENTRI OpenCode ingestion adapter (ROADMAP 3b.3).

Tails an external ``opencode.db`` — the SQLite store OpenCode keeps for its own
sessions/messages — into ``ingest.opencode.message`` spine events. This makes
coding work a developer did *directly* in OpenCode (outside CENTRI's hand path)
visible to the memory graph: each ingested message becomes an event, and the
consolidation worker folds the per-session digest into a typed fact exactly as
it does for native ``hand.transcript`` events.

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

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# Candidate column names, most-specific first. The reader picks the first one
# present on the message table.
_ID_COLS = ("id", "message_id", "messageID", "msg_id")
_SESSION_COLS = ("session_id", "sessionID", "session", "sessionId", "thread_id")
_ROLE_COLS = ("role", "type", "author", "sender")
_CONTENT_COLS = ("content", "text", "body", "message", "parts")
_TS_COLS = ("created_at", "createdAt", "created", "time_created", "time", "ts", "timestamp")
_MESSAGE_TABLES = ("message", "messages", "Message")

_SUMMARY_CHARS = 240
_FACT_STATEMENT_CHARS = 400


def _first_present(available: List[str], candidates: Tuple[str, ...]) -> Optional[str]:
    lower = {c.lower(): c for c in available}
    for cand in candidates:
        if cand.lower() in lower:
            return lower[cand.lower()]
    return None


def _coerce_ts(value: Any) -> str:
    """Normalize a row timestamp to an ISO-8601 string for stable ordering.

    OpenCode has stored timestamps as ISO strings and as epoch ms/seconds; both
    are accepted. Unparseable values sort lexicographically as-is.
    """
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        # Heuristic: ms vs seconds. 1e12 ~ 2001 in ms / year 33658 in seconds.
        seconds = value / 1000.0 if value > 1_000_000_000_000 else float(value)
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()
        except (ValueError, OverflowError, OSError):
            return str(value)
    return str(value)


def _flatten_content(raw: Any) -> str:
    """Turn a message content cell into plain text.

    Plain strings pass through. OpenCode sometimes stores a JSON array of
    "parts" ({"type": "text", "text": ...}); those are flattened to their text.
    """
    if raw is None:
        return ""
    if isinstance(raw, (int, float)):
        return str(raw)
    if isinstance(raw, str):
        stripped = raw.strip()
        if stripped.startswith("[") or stripped.startswith("{"):
            try:
                parsed = json.loads(stripped)
                return _flatten_content(parsed)
            except (ValueError, TypeError):
                return raw
        return raw
    if isinstance(raw, list):
        parts: List[str] = []
        for item in raw:
            if isinstance(item, dict):
                text = item.get("text") or item.get("content") or item.get("value")
                if text:
                    parts.append(str(text))
            elif item:
                parts.append(str(item))
        return "\n".join(parts)
    if isinstance(raw, dict):
        text = raw.get("text") or raw.get("content") or raw.get("value")
        return str(text) if text else ""
    return str(raw)


class OpenCodeIngestor:
    """Incremental, idempotent tail of an ``opencode.db`` into spine events."""

    def __init__(self, db: Any, event_bus: Any = None):
        self._db = db
        self._event_bus = event_bus

    async def ingest(
        self,
        opencode_db_path: str | Path,
        source: Optional[str] = None,
        repo_id: Optional[str] = None,
        limit: int = 5000,
    ) -> Dict[str, Any]:
        """Read new messages from ``opencode_db_path`` and append spine events.

        ``source`` keys the high-water mark; it defaults to the absolute db path
        so distinct stores tail independently. Returns a summary dict
        ``{source, ingested, high_water, scanned}``. Idempotent: a second call
        with no new rows ingests nothing.
        """
        path = Path(opencode_db_path)
        src = source or f"opencode:{path.resolve()}"
        if not path.exists():
            logger.info("OpenCode ingest: db not found at %s (source=%s)", path, src)
            return {"source": src, "ingested": 0, "high_water": "", "scanned": 0, "available": False}

        high_water = await self._db.get_ingest_high_water(src)
        try:
            rows = self._read_messages(path)
        except Exception as exc:
            logger.warning("OpenCode ingest read failed for %s: %s", path, exc)
            return {"source": src, "ingested": 0, "high_water": high_water, "scanned": 0, "available": False, "error": str(exc)}

        # Sort by (ts, id) so the cursor is total-order stable across collisions.
        rows.sort(key=lambda r: (r["ts"], str(r["id"])))
        scanned = len(rows)

        ingested = 0
        newest_cursor = high_water
        for row in rows:
            cursor = f"{row['ts']}|{row['id']}"
            if high_water and cursor <= high_water:
                continue
            wrote = await self._append_message_event(src, row, repo_id)
            if wrote:
                ingested += 1
            if cursor > newest_cursor:
                newest_cursor = cursor

        if ingested or newest_cursor != high_water:
            await self._db.set_ingest_high_water(
                src, newest_cursor, last_run_at=_now(), ingested_delta=ingested
            )
        return {
            "source": src,
            "ingested": ingested,
            "high_water": newest_cursor,
            "scanned": scanned,
            "available": True,
        }

    # ------------------------------------------------------------------
    # External DB read (read-only)
    # ------------------------------------------------------------------
    def _read_messages(self, path: Path) -> List[Dict[str, Any]]:
        uri = f"file:{path.resolve()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            conn.row_factory = sqlite3.Row
            table = self._resolve_table(conn)
            if table is None:
                raise ValueError("no message table found in opencode.db")
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
            id_col = _first_present(cols, _ID_COLS)
            session_col = _first_present(cols, _SESSION_COLS)
            role_col = _first_present(cols, _ROLE_COLS)
            content_col = _first_present(cols, _CONTENT_COLS)
            ts_col = _first_present(cols, _TS_COLS)

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
                        "content": _flatten_content(rd.get(content_col)) if content_col else "",
                        "ts": _coerce_ts(rd.get(ts_col)) if ts_col else "",
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
        # Fall back to any table that has a content-like and a session-like col.
        for name in names:
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({name})").fetchall()]
            if _first_present(cols, _CONTENT_COLS) and _first_present(cols, _ROLE_COLS):
                return name
        return None

    # ------------------------------------------------------------------
    # Spine write
    # ------------------------------------------------------------------
    async def _append_message_event(
        self, source: str, row: Dict[str, Any], repo_id: Optional[str]
    ) -> bool:
        # Deterministic id => idempotent even if the high-water mark is reset.
        event_id = f"ingest:{source}:{row['id']}"
        if await self._db.event_exists(event_id):
            return False

        content = row.get("content") or ""
        session_id = row.get("session_id") or ""
        role = row.get("role") or "unknown"
        ts = row.get("ts") or _now()

        summary = content.strip().replace("\n", " ")[:_SUMMARY_CHARS]
        payload: Dict[str, Any] = {
            "tool": "opencode",
            "ingest_source": source,
            "external_id": row["id"],
            "session_uid": session_id,
            "role": role,
            "text": content,
            "summary": summary,
        }
        # Carry a deterministic consolidation hint so the ingested session
        # surfaces as a typed fact in briefs — same contract as hand.transcript.
        # Only assistant/tool output carries durable signal worth a fact; user
        # prompts and empty rows are recorded as events but not folded.
        if content.strip() and role.lower() in ("assistant", "tool", "model"):
            topic = f"opencode-session:{session_id or row['id']}"
            payload["fact"] = {
                "topic": topic,
                "statement": (
                    f"OpenCode session {session_id or row['id']} ({role}): "
                    f"{content.strip()[:_FACT_STATEMENT_CHARS]}"
                ),
                "tags": ["ingest", "opencode", "transcript"],
            }

        try:
            await self._db.append_event(
                event_id=event_id,
                type="ingest.opencode.message",
                source="ingest.opencode",
                ts=ts if isinstance(ts, str) and ts else _now(),
                thread_id=None,
                task_id=None,
                repo_id=repo_id,
                importance="low",
                payload=payload,
            )
        except sqlite3.IntegrityError:
            # Lost a race / duplicate id: idempotency holds, count as not-written.
            return False
        except Exception:
            logger.debug("ingest event write failed for %s", event_id, exc_info=True)
            return False

        if self._event_bus is not None:
            try:
                await self._event_bus.publish(
                    {
                        "id": event_id,
                        "type": "ingest.opencode.message",
                        "ts": ts if isinstance(ts, str) and ts else _now(),
                        "source": "ingest.opencode",
                        "repo_id": repo_id,
                        "importance": "low",
                        "payload": payload,
                        "summary": summary,
                    }
                )
            except Exception:
                logger.debug("ingest event publish failed for %s", event_id, exc_info=True)
        return True


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
