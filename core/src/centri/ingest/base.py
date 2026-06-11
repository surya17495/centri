"""Shared ingestion-adapter machinery (ROADMAP 3b.4).

3b.3 shipped a single ``OpenCodeIngestor``. 3b.4 generalizes it into an *adapter
registry*: per-agent adapters that all share one HWM / idempotency / redaction /
event-write core, so adding a new coding agent is a reader plus a few labels, not
a new ingest pipeline.

A :class:`MessageAdapter` subclass supplies three things:

  - ``tool`` / ``event_type`` / ``event_source`` / ``source_prefix`` — the labels
    that distinguish one agent's events from another's.
  - ``read_messages(spec)`` — read the agent's external store and return a list of
    normalized ``{id, session_id, role, content, ts}`` dicts (read-only).
  - ``default_locations()`` — well-known default paths to probe during discovery.

Everything else (per-source high-water mark, deterministic ids + ``event_exists``
guard, the ``fact`` hint for assistant/tool output, redaction via
``db.append_event``, the live publish) lives here and is identical across agents.

Bootstrap is *first tick*: because ingestion is HWM-based, a one-time import on a
fresh install and the continuous ambient tail are literally the same code path —
a full import is just an ingest run against a source whose high-water mark is the
empty string.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


_SUMMARY_CHARS = 240
_FACT_STATEMENT_CHARS = 400

# Roles whose output carries durable signal worth folding into a typed fact.
# User prompts are captured as events (completeness) but never folded — a bare
# question is not a decision, and consolidation must not confabulate one.
_FOLDABLE_ROLES = ("assistant", "tool", "model")


def first_present(available: List[str], candidates: Tuple[str, ...]) -> Optional[str]:
    """First candidate column name present (case-insensitive) in ``available``."""
    lower = {c.lower(): c for c in available}
    for cand in candidates:
        if cand.lower() in lower:
            return lower[cand.lower()]
    return None


def coerce_ts(value: Any) -> str:
    """Normalize a row timestamp to an ISO-8601 string for stable ordering.

    Accepts ISO strings and epoch ms/seconds; unparseable values sort
    lexicographically as-is.
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


def flatten_content(raw: Any) -> str:
    """Turn a message content cell into plain text.

    Plain strings pass through. JSON arrays of "parts"
    (``{"type": "text", "text": ...}``) are flattened to their text — both
    OpenCode and Claude Code store content this way in some versions.
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
                return flatten_content(parsed)
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


@dataclass
class IngestSpec:
    """What to ingest from one source.

    ``path`` is the external store (a db file or a directory of JSONL files,
    depending on the agent). ``source`` keys the high-water mark; it defaults to
    ``<source_prefix>:<resolved-path>`` so distinct stores tail independently.
    """

    path: Path
    source: Optional[str] = None
    repo_id: Optional[str] = None
    limit: int = 5000


@dataclass
class DiscoveredSource:
    """A located external store, returned by discovery.

    ``available`` is False with a ``reason`` when the expected store is absent or
    unreadable — discovery degrades honestly rather than pretending. ``count`` is
    a cheap message count where the adapter can get one without a full read.
    """

    agent: str
    path: str
    available: bool
    source: str = ""
    count: Optional[int] = None
    reason: str = ""

    def as_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "agent": self.agent,
            "path": self.path,
            "available": self.available,
            "source": self.source,
        }
        if self.count is not None:
            d["count"] = self.count
        if self.reason:
            d["reason"] = self.reason
        return d


class MessageAdapter:
    """Base for per-agent ingestion adapters.

    Subclasses set the label attributes and implement :meth:`read_messages` and
    :meth:`default_locations`. The HWM/idempotency/redaction/write core is shared.
    """

    # Labels — overridden per agent.
    agent: str = "base"
    tool: str = "base"
    event_type: str = "ingest.base.message"
    event_source: str = "ingest.base"
    source_prefix: str = "base"
    fact_tags: Tuple[str, ...] = ("ingest", "transcript")

    def __init__(self, db: Any, event_bus: Any = None):
        self._db = db
        self._event_bus = event_bus

    # ------------------------------------------------------------------
    # Subclass hooks
    # ------------------------------------------------------------------
    def read_messages(self, path: Path) -> List[Dict[str, Any]]:
        """Read the external store and return normalized message dicts.

        Each dict: ``{id, session_id, role, content, ts}`` (all strings except
        the optional structured ``ts``). Read-only. Raise on a genuinely broken
        store; return ``[]`` for an empty-but-valid one.
        """
        raise NotImplementedError

    def default_locations(self) -> List[Path]:
        """Well-known default paths to probe during discovery, most-likely first."""
        return []

    def fact_topic(self, row: Dict[str, Any]) -> str:
        session_id = row.get("session_id") or row.get("id")
        return f"{self.tool}-session:{session_id}"

    def fact_statement(self, row: Dict[str, Any]) -> str:
        session_id = row.get("session_id") or row.get("id")
        role = row.get("role") or "unknown"
        content = (row.get("content") or "").strip()
        return f"{self.tool} session {session_id} ({role}): {content[:_FACT_STATEMENT_CHARS]}"

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------
    def discover(self, extra_paths: Optional[List[str]] = None) -> List[DiscoveredSource]:
        """Probe default + configured paths; report what is found (read-only).

        Honest-unavailable: a probed path that does not exist or cannot be
        counted is returned with ``available=False`` and a ``reason`` rather than
        omitted, so a caller sees *why* nothing was found.
        """
        out: List[DiscoveredSource] = []
        seen: set[str] = set()
        candidates: List[Path] = list(self.default_locations())
        for p in extra_paths or []:
            candidates.append(Path(p).expanduser())
        for path in candidates:
            resolved = str(path.resolve()) if path.exists() else str(path)
            if resolved in seen:
                continue
            seen.add(resolved)
            src = f"{self.source_prefix}:{resolved}"
            if not path.exists():
                continue  # absent default path is not noise; skip silently
            count, reason = self._cheap_count(path)
            out.append(
                DiscoveredSource(
                    agent=self.agent,
                    path=resolved,
                    available=reason == "",
                    source=src,
                    count=count,
                    reason=reason,
                )
            )
        return out

    def _cheap_count(self, path: Path) -> Tuple[Optional[int], str]:
        """Best-effort message count for discovery. Returns (count, reason).

        Default: do a full read and count (subclasses with a cheaper path —
        e.g. ``SELECT count(*)`` — override). A non-empty ``reason`` marks the
        source unavailable.
        """
        try:
            rows = self.read_messages(path)
            return len(rows), ""
        except Exception as exc:  # noqa: BLE001 — degrade honestly
            return None, f"unreadable: {exc}"

    # ------------------------------------------------------------------
    # Ingest core (shared) — incremental + idempotent
    # ------------------------------------------------------------------
    def default_source(self, path: Path) -> str:
        return f"{self.source_prefix}:{path.resolve()}"

    async def ingest(
        self,
        path: str | Path,
        source: Optional[str] = None,
        repo_id: Optional[str] = None,
        limit: int = 5000,
    ) -> Dict[str, Any]:
        """Read new messages from ``path`` and append spine events.

        ``source`` keys the high-water mark (default: ``<prefix>:<abs-path>``).
        Returns ``{agent, source, ingested, high_water, scanned, available}``.
        Idempotent: a second pass with no new rows ingests nothing.
        """
        p = Path(path)
        src = source or self.default_source(p)
        if not p.exists():
            logger.info("%s ingest: store not found at %s (source=%s)", self.agent, p, src)
            return {
                "agent": self.agent,
                "source": src,
                "ingested": 0,
                "high_water": "",
                "scanned": 0,
                "available": False,
            }

        high_water = await self._db.get_ingest_high_water(src)
        try:
            rows = self.read_messages(p)
        except Exception as exc:  # noqa: BLE001 — degrade honestly
            logger.warning("%s ingest read failed for %s: %s", self.agent, p, exc)
            return {
                "agent": self.agent,
                "source": src,
                "ingested": 0,
                "high_water": high_water,
                "scanned": 0,
                "available": False,
                "error": str(exc),
            }

        # Sort by (ts, id) so the cursor is a stable total order across collisions.
        rows.sort(key=lambda r: (r.get("ts") or "", str(r.get("id"))))
        scanned = len(rows)

        ingested = 0
        newest_cursor = high_water
        for row in rows:
            cursor = f"{row.get('ts') or ''}|{row.get('id')}"
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
            "agent": self.agent,
            "source": src,
            "ingested": ingested,
            "high_water": newest_cursor,
            "scanned": scanned,
            "available": True,
        }

    # ------------------------------------------------------------------
    # Spine write (shared)
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
            "tool": self.tool,
            "ingest_source": source,
            "external_id": row["id"],
            "session_uid": session_id,
            "role": role,
            "text": content,
            "summary": summary,
        }
        # Carry a deterministic consolidation hint so the ingested session
        # surfaces as a typed fact in briefs — same contract as hand.transcript.
        if content.strip() and role.lower() in _FOLDABLE_ROLES:
            payload["fact"] = {
                "topic": self.fact_topic(row),
                "statement": self.fact_statement(row),
                "tags": list(self.fact_tags),
            }

        ts_str = ts if isinstance(ts, str) and ts else _now()
        try:
            await self._db.append_event(
                event_id=event_id,
                type=self.event_type,
                source=self.event_source,
                ts=ts_str,
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
                        "type": self.event_type,
                        "ts": ts_str,
                        "source": self.event_source,
                        "repo_id": repo_id,
                        "importance": "low",
                        "payload": payload,
                        "summary": summary,
                    }
                )
            except Exception:
                logger.debug("ingest event publish failed for %s", event_id, exc_info=True)
        return True
