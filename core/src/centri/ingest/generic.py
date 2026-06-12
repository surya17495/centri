"""Generic config-driven fallback ingestion adapter (ROADMAP 3b.5).

3b.4 shipped adapters for the agents we know (OpenCode, Claude Code, Cursor). But
the long tail of coding agents store transcripts in one of two boringly common
shapes:

  - **JSONL** — one JSON object per line, with role/content/timestamp under
    *some* field names.
  - **SQLite chat table** — a table with role/content/timestamp *columns*.

Rather than write a new adapter per agent, this one is **config-driven**: you tell
it the field/column names and point it at a file or db, and it reuses the
registry's HWM / idempotency / redaction / write core exactly like the built-in
adapters. This is the "adapter contract" made concrete — see
``docs/ingestion-adapters.md``.

It is read-only and schema-tolerant: configured field names are tried first, then
the same generous candidate lists the built-in adapters use, so a slightly-off
config still finds data. When neither a JSONL file nor a SQLite chat table is
present it degrades honestly (raises, surfaced as unavailable by discovery).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from centri.ingest.base import (
    MessageAdapter,
    coerce_ts,
    first_present,
    flatten_content,
)

logger = logging.getLogger(__name__)

# Fallback candidate field/column names (used when the config omits one).
_ID_CANDS = ("id", "message_id", "messageId", "uuid", "rowid")
_SESSION_CANDS = ("session_id", "sessionId", "session", "conversation_id", "thread_id")
_ROLE_CANDS = ("role", "type", "author", "sender")
_CONTENT_CANDS = ("content", "text", "body", "message")
_TS_CANDS = ("timestamp", "ts", "created_at", "createdAt", "time", "created")


@dataclass
class GenericAdapterConfig:
    """Field/column mapping for a generic source.

    ``kind`` is ``"jsonl"`` or ``"sqlite"``. For sqlite, ``table`` names the chat
    table (auto-resolved if omitted). Field/column names default to None, in which
    case the adapter falls back to the built-in candidate lists.
    """

    agent: str = "generic"
    kind: str = "jsonl"  # "jsonl" | "sqlite"
    table: Optional[str] = None
    id_field: Optional[str] = None
    session_field: Optional[str] = None
    role_field: Optional[str] = None
    content_field: Optional[str] = None
    ts_field: Optional[str] = None
    locations: List[str] = field(default_factory=list)


class GenericIngestor(MessageAdapter):
    """Config-driven JSONL / SQLite chat-table adapter for unknown agents."""

    fact_tags = ("ingest", "generic", "transcript")

    def __init__(self, db: Any, config: GenericAdapterConfig, event_bus: Any = None):
        super().__init__(db, event_bus=event_bus)
        self._cfg = config
        # Per-instance labels so two generic sources don't collide.
        self.agent = config.agent
        self.tool = config.agent
        self.event_type = f"ingest.{config.agent}.message"
        self.event_source = f"ingest.{config.agent}"
        self.source_prefix = config.agent

    def default_locations(self) -> List[Path]:
        return [Path(p).expanduser() for p in self._cfg.locations]

    # ------------------------------------------------------------------
    # Read dispatch
    # ------------------------------------------------------------------
    def read_messages(self, path: Path) -> List[Dict[str, Any]]:
        if self._cfg.kind == "sqlite":
            return self._read_sqlite(path)
        return self._read_jsonl(path)

    # -- JSONL -----------------------------------------------------------
    def _jsonl_files(self, path: Path) -> List[Path]:
        if path.is_file():
            return [path]
        if path.is_dir():
            return sorted(path.rglob("*.jsonl"))
        return []

    def _pick(self, obj: Dict[str, Any], configured: Optional[str], cands: Tuple[str, ...]) -> Any:
        if configured and configured in obj and obj[configured] not in (None, ""):
            return obj[configured]
        for c in cands:
            if c in obj and obj[c] not in (None, ""):
                return obj[c]
        return None

    def _read_jsonl(self, path: Path) -> List[Dict[str, Any]]:
        files = self._jsonl_files(path)
        if not files:
            if path.is_dir():
                return []  # empty-but-valid
            raise ValueError(f"no JSONL file at {path}")
        out: List[Dict[str, Any]] = []
        for fp in files:
            try:
                text = fp.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                logger.debug("generic jsonl: cannot read %s: %s", fp, exc)
                continue
            for lineno, line in enumerate(text.splitlines()):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(obj, dict):
                    continue
                rid = self._pick(obj, self._cfg.id_field, _ID_CANDS)
                if rid in (None, ""):
                    rid = f"{fp.stem}:{lineno}"
                session = self._pick(obj, self._cfg.session_field, _SESSION_CANDS) or fp.stem
                role = self._pick(obj, self._cfg.role_field, _ROLE_CANDS) or ""
                content = flatten_content(self._pick(obj, self._cfg.content_field, _CONTENT_CANDS))
                ts = coerce_ts(self._pick(obj, self._cfg.ts_field, _TS_CANDS))
                out.append(
                    {
                        "id": str(rid),
                        "session_id": str(session),
                        "role": str(role),
                        "content": content,
                        "ts": ts,
                    }
                )
        return out

    # -- SQLite ----------------------------------------------------------
    def _resolve_table(self, conn: sqlite3.Connection) -> Optional[str]:
        if self._cfg.table:
            return self._cfg.table
        names = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        for name in names:
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({name})").fetchall()]
            if first_present(cols, _CONTENT_CANDS) and first_present(cols, _ROLE_CANDS):
                return name
        return None

    def _read_sqlite(self, path: Path) -> List[Dict[str, Any]]:
        uri = f"file:{path.resolve()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            conn.row_factory = sqlite3.Row
            table = self._resolve_table(conn)
            if table is None:
                raise ValueError("no chat table found in sqlite store")
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
            id_col = self._cfg.id_field if self._cfg.id_field in cols else first_present(cols, _ID_CANDS)
            session_col = (
                self._cfg.session_field if self._cfg.session_field in cols
                else first_present(cols, _SESSION_CANDS)
            )
            role_col = self._cfg.role_field if self._cfg.role_field in cols else first_present(cols, _ROLE_CANDS)
            content_col = (
                self._cfg.content_field if self._cfg.content_field in cols
                else first_present(cols, _CONTENT_CANDS)
            )
            ts_col = self._cfg.ts_field if self._cfg.ts_field in cols else first_present(cols, _TS_CANDS)
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
