"""CENTRI Cursor ingestion adapter (ROADMAP 3b.4).

Cursor (a VS Code fork) keeps its local state in SQLite ``state.vscdb`` files —
one global, plus one per workspace under ``workspaceStorage/<hash>/state.vscdb``.
Chat/conversation data lives in a key-value table (``ItemTable`` or
``cursorDiskKV``: a ``key`` TEXT, ``value`` BLOB/TEXT pair) under
Cursor-specific keys whose JSON values hold message arrays. The exact key names
and JSON shape change often between Cursor releases, so this adapter is **maximally
schema-tolerant**: it scans every value, parses JSON, and harvests anything that
looks like a chat message ``{role/type, text/content}``. When no KV table is
present it **degrades honestly** — logs a reason and reports the source
unavailable rather than raising.

Read path is **read-only** (``mode=ro``) and **idempotent** (deterministic event
ids keyed off a stable per-message id + a per-source high-water mark). Emits
``ingest.cursor.message`` events; assistant/tool turns carry a ``fact`` hint that
consolidation folds, user prompts are captured but not folded — same contract as
the OpenCode and Claude Code adapters.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from centri.ingest.base import (
    MessageAdapter,
    coerce_ts,
    flatten_content,
)

logger = logging.getLogger(__name__)

# Candidate key-value tables Cursor has used for chat state.
_KV_TABLES = ("ItemTable", "cursorDiskKV", "kv", "items")
# Keys whose names hint at chat/conversation/composer content.
_CHAT_KEY_HINTS = ("chat", "composer", "conversation", "message", "aichat", "bubble")
_ROLE_KEYS = ("role", "type", "author", "sender", "speaker")
_TEXT_KEYS = ("text", "content", "body", "message", "richText")
_TS_KEYS = ("timestamp", "ts", "createdAt", "created_at", "time")
_ID_KEYS = ("id", "bubbleId", "messageId", "uuid", "key")


def _cursor_default_locations() -> List[Path]:
    """Well-known Cursor state roots per platform (macOS/Linux)."""
    home = Path.home()
    roots: List[Path] = []
    if sys.platform == "darwin":
        roots.append(home / "Library" / "Application Support" / "Cursor" / "User")
    else:
        # Linux: XDG config.
        roots.append(home / ".config" / "Cursor" / "User")
    paths: List[Path] = []
    for root in roots:
        paths.append(root / "globalStorage" / "state.vscdb")
        paths.append(root / "workspaceStorage")  # dir of per-workspace state.vscdb
    return paths


def _harvest_messages(value: Any, key: str) -> List[Dict[str, Any]]:
    """Pull message-shaped dicts out of an arbitrary decoded JSON value.

    Recurses into lists and dict values, collecting any dict that has a
    text-like field. Role/timestamp/id are read tolerantly; missing fields are
    fine. ``key`` seeds id/session fallbacks so harvesting is deterministic.
    """
    out: List[Dict[str, Any]] = []

    def _looks_like_message(d: Dict[str, Any]) -> bool:
        return any(k in d for k in _TEXT_KEYS)

    def _first(d: Dict[str, Any], keys: Tuple[str, ...]) -> Any:
        for k in keys:
            if k in d and d[k] not in (None, ""):
                return d[k]
        return None

    def _walk(node: Any, trail: str) -> None:
        if isinstance(node, dict):
            if _looks_like_message(node):
                text = flatten_content(_first(node, _TEXT_KEYS))
                if text.strip():
                    rid = _first(node, _ID_KEYS) or trail
                    out.append(
                        {
                            "id": str(rid),
                            "session_id": str(_first(node, ("conversationId", "composerId", "sessionId")) or key),
                            "role": str(_first(node, _ROLE_KEYS) or ""),
                            "content": text,
                            "ts": coerce_ts(_first(node, _TS_KEYS)),
                        }
                    )
            for k, v in node.items():
                _walk(v, f"{trail}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                _walk(v, f"{trail}[{i}]")

    _walk(value, key)
    return out


class CursorIngestor(MessageAdapter):
    """Incremental, idempotent, schema-tolerant tail of Cursor ``state.vscdb``."""

    agent = "cursor"
    tool = "cursor"
    event_type = "ingest.cursor.message"
    event_source = "ingest.cursor"
    source_prefix = "cursor"
    fact_tags = ("ingest", "cursor", "transcript")

    def default_locations(self) -> List[Path]:
        return _cursor_default_locations()

    def fact_topic(self, row: Dict[str, Any]) -> str:
        session_id = row.get("session_id") or row.get("id")
        return f"cursor-session:{session_id}"

    def fact_statement(self, row: Dict[str, Any]) -> str:
        session_id = row.get("session_id") or row.get("id")
        role = row.get("role") or "unknown"
        content = (row.get("content") or "").strip()
        return f"Cursor session {session_id} ({role}): {content[:400]}"

    # ------------------------------------------------------------------
    # External read (read-only)
    # ------------------------------------------------------------------
    def _vscdb_files(self, path: Path) -> List[Path]:
        if path.is_file():
            return [path]
        if path.is_dir():
            # workspaceStorage/<hash>/state.vscdb (one per workspace).
            return sorted(path.rglob("state.vscdb"))
        return []

    def _resolve_kv_table(self, conn: sqlite3.Connection) -> Optional[Tuple[str, str, str]]:
        """Return (table, key_col, value_col) for a key-value table, or None."""
        names = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        for cand in _KV_TABLES:
            if cand in names:
                cols = [r[1].lower() for r in conn.execute(f"PRAGMA table_info({cand})").fetchall()]
                key_col = "key" if "key" in cols else (cols[0] if cols else None)
                val_col = "value" if "value" in cols else (cols[1] if len(cols) > 1 else None)
                if key_col and val_col:
                    return cand, key_col, val_col
        # Fall back to any 2-column (key,value)-shaped table.
        for name in names:
            cols = [r[1].lower() for r in conn.execute(f"PRAGMA table_info({name})").fetchall()]
            if "key" in cols and "value" in cols:
                return name, "key", "value"
        return None

    def read_messages(self, path: Path) -> List[Dict[str, Any]]:
        files = self._vscdb_files(path)
        if not files:
            raise ValueError("no state.vscdb found")
        out: List[Dict[str, Any]] = []
        any_kv = False
        for fp in files:
            uri = f"file:{fp.resolve()}?mode=ro"
            conn = sqlite3.connect(uri, uri=True)
            try:
                resolved = self._resolve_kv_table(conn)
                if resolved is None:
                    logger.info("cursor: no key-value table in %s (skipping)", fp)
                    continue
                any_kv = True
                table, key_col, val_col = resolved
                rows = conn.execute(f"SELECT {key_col}, {val_col} FROM {table}").fetchall()
                for k, v in rows:
                    key = str(k or "")
                    if not any(h in key.lower() for h in _CHAT_KEY_HINTS):
                        continue
                    if isinstance(v, (bytes, bytearray)):
                        try:
                            v = v.decode("utf-8", errors="replace")
                        except Exception:  # noqa: BLE001
                            continue
                    if not isinstance(v, str) or not v.strip():
                        continue
                    try:
                        decoded = json.loads(v)
                    except (ValueError, TypeError):
                        continue
                    # Seed id with the workspace file + KV key so ids stay unique
                    # and deterministic across multiple state.vscdb files.
                    out.extend(_harvest_messages(decoded, f"{fp.stem}:{key}"))
            finally:
                conn.close()
        if not any_kv:
            # Every probed db lacked a KV table — degrade honestly.
            raise ValueError("no key-value (chat) table found in any state.vscdb")
        return out

    def _cheap_count(self, path: Path) -> Tuple[Optional[int], str]:
        try:
            rows = self.read_messages(path)
            return len(rows), ""
        except Exception as exc:  # noqa: BLE001 — degrade honestly
            return None, f"unreadable: {exc}"
