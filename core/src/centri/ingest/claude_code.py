"""CENTRI Claude Code ingestion adapter (ROADMAP 3b.4).

Claude Code stores each project's session transcript as newline-delimited JSON
(JSONL) under ``~/.claude`` — typically
``~/.claude/projects/<project-slug>/<session-uuid>.jsonl``, one JSON object per
turn. This adapter tails those files into ``ingest.claude_code.message`` spine
events so coding work a developer did in Claude Code (outside CENTRI's hand path)
becomes visible to the memory graph, exactly like the OpenCode adapter does for
``opencode.db``.

Read path is **read-only** and **idempotent** (deterministic event ids + a
per-source high-water mark). The JSONL schema has varied across Claude Code
versions, so the reader is tolerant:

  - id        — ``uuid`` / ``id`` / ``messageId``; falls back to ``<file>:<line>``
  - session   — ``sessionId`` / ``session_id``; falls back to the file stem
  - role      — ``role`` / ``type`` (``message.role`` when nested)
  - content   — ``content`` / ``text`` / ``message.content``; JSON "parts"
                arrays (``{"type":"text","text":...}``) are flattened
  - timestamp — ``timestamp`` / ``ts`` / ``created_at``

Assistant/tool turns carry a ``fact`` hint (folded by consolidation); user
prompts are captured as events but not folded — same contract as 3b.3.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from centri.ingest.base import (
    MessageAdapter,
    coerce_ts,
    flatten_content,
)

logger = logging.getLogger(__name__)


def _claude_default_locations() -> List[Path]:
    """Well-known ``~/.claude`` project-transcript roots (macOS/Linux share)."""
    home = Path.home()
    # The projects dir holds per-project session JSONL; the root is the fallback.
    return [home / ".claude" / "projects", home / ".claude"]


def _dig(obj: Any, *keys: str) -> Any:
    """Return the first present key from a dict (top-level or one level deep)."""
    if not isinstance(obj, dict):
        return None
    for k in keys:
        if k in obj and obj[k] not in (None, ""):
            return obj[k]
    # one level of nesting under "message"
    msg = obj.get("message")
    if isinstance(msg, dict):
        for k in keys:
            if k in msg and msg[k] not in (None, ""):
                return msg[k]
    return None


class ClaudeCodeIngestor(MessageAdapter):
    """Incremental, idempotent tail of ``~/.claude`` session JSONL into the spine."""

    agent = "claude_code"
    tool = "claude_code"
    event_type = "ingest.claude_code.message"
    event_source = "ingest.claude_code"
    source_prefix = "claude_code"
    fact_tags = ("ingest", "claude_code", "transcript")

    def default_locations(self) -> List[Path]:
        return _claude_default_locations()

    def fact_topic(self, row: Dict[str, Any]) -> str:
        session_id = row.get("session_id") or row.get("id")
        return f"claude_code-session:{session_id}"

    def fact_statement(self, row: Dict[str, Any]) -> str:
        session_id = row.get("session_id") or row.get("id")
        role = row.get("role") or "unknown"
        content = (row.get("content") or "").strip()
        return f"Claude Code session {session_id} ({role}): {content[:400]}"

    # ------------------------------------------------------------------
    # External read (read-only): walk *.jsonl under the path
    # ------------------------------------------------------------------
    def _jsonl_files(self, path: Path) -> List[Path]:
        if path.is_file():
            return [path] if path.suffix == ".jsonl" else []
        if path.is_dir():
            return sorted(path.rglob("*.jsonl"))
        return []

    def read_messages(self, path: Path) -> List[Dict[str, Any]]:
        files = self._jsonl_files(path)
        if not files and path.is_dir():
            return []  # empty-but-valid directory
        if not files and path.is_file():
            return []  # non-jsonl file: nothing to read
        out: List[Dict[str, Any]] = []
        for fp in files:
            file_stem = fp.stem
            try:
                text = fp.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                logger.debug("claude_code: cannot read %s: %s", fp, exc)
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
                rid = _dig(obj, "uuid", "id", "messageId")
                if rid in (None, ""):
                    # Stable fallback id so re-runs stay idempotent per line.
                    rid = f"{file_stem}:{lineno}"
                session_id = _dig(obj, "sessionId", "session_id", "session") or file_stem
                role = _dig(obj, "role", "type", "author") or ""
                content_raw = _dig(obj, "content", "text", "body")
                content = flatten_content(content_raw)
                ts = coerce_ts(_dig(obj, "timestamp", "ts", "created_at", "createdAt"))
                out.append(
                    {
                        "id": str(rid),
                        "session_id": str(session_id),
                        "role": str(role),
                        "content": content,
                        "ts": ts,
                    }
                )
        return out

    def _cheap_count(self, path: Path) -> Tuple[Optional[int], str]:
        files = self._jsonl_files(path)
        if not files:
            if path.is_dir():
                return 0, ""
            return None, "no session JSONL files found"
        # Counting lines is cheap enough and avoids JSON-parsing every row.
        total = 0
        for fp in files:
            try:
                with fp.open("r", encoding="utf-8", errors="replace") as fh:
                    total += sum(1 for ln in fh if ln.strip())
            except OSError as exc:
                return None, f"unreadable: {exc}"
        return total, ""
