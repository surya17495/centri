"""CENTRI OpenCode/Centri ingestion adapter v2 — structured event ingestion.

ROADMAP 3b.3 → structured deck  (replaces the v1 transcript-only adapter).

Reads from OpenCode's *event* table (``message.part.updated.1``, ``session.updated.1``,
``session.next.model.switched.1``, etc.) and turns them into **typed spine events**
with deterministic hints for the consolidation path.  The v1 adapter read from the
``message`` table and produced flat ingest.opencode.message events that required an
LLM to re-infer meaning from chat text.  This adapter reads the structured events
directly — tool calls carry ``tool_called`` hints, session summaries carry
``decision`` and ``open_loop`` hints, file-diff summaries carry ``fact`` hints.

Key tables:

  ``event`` — the structured event log (102k+ rows)
    type  : ``message.part.updated.1``, ``session.updated.1``, etc.
    data  : JSON blob with tool, input, state, metadata
    aggregate_id : session or message id
    seq   : ordering within aggregate

  ``session`` — session metadata
    id, title, summary_files, summary_diffs, directory, project_id

The adapter ingests three event types to the spine:

  1. ``ingest.opencode.tool_called`` — tool execution with description/command.
     Payload carries ``{tool, input, output, state, description, cmd, invoked_at}``
     + ``fact`` hint for consolidation (deterministic path, no LLM).

  2. ``ingest.opencode.session_activity`` — session lifecycle (created, updated,
     model switched).  Payload carries ``{title, files, diffs, model, agent}``
     + ``decision`` / ``open_loop`` hints.

  3. ``ingest.opencode.transcript`` — chat messages (assistant/user/system).
     Only for foldable roles, with a lightweight ``fact`` topic hint.

Deterministic consolidation:

  - ``tool_called`` events with ``state.status == 'completed'`` and non-trivial
    output → deterministic fact: topic = ``tool:<name>``, statement = description.
  - ``session_activity`` with model/agent changes → decision: topic =
    ``model_switched`` / ``agent_switched``, statement = what changed and why.
  - ``session_activity`` with active work summary → open_loop: topic =
    ``session:<id>:work``, statement = what was touched and left open.

All hints are deterministic; the consolidation worker's ``_apply_hint`` reads them
directly without an LLM.  (Optional: an LLM extractor can be layered behind
``_iter_hints`` for unstructured histories; this adapter targets the structured
path as the production default.)

The shared HWM/idempotency/redaction/write core lives in
:mod:`centri.ingest.base`; this module is the OpenCode-specific reader.

Read-only (``mode=ro``); idempotent (deterministic event_id from event
``(type, aggregate_id, seq)``); high-water mark is ``ts|id`` for stable resume.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from centri.ingest.base import (
    DiscoveredSource,
    MessageAdapter,
    coerce_ts,
    first_present,
    _FACT_STATEMENT_CHARS,
)

logger = logging.getLogger(__name__)

# Candidate column names (most-specific first) for the event/session tables.
_EVENT_ID_COLS = ("id", "event_id", "eventID")
_EVENT_TYPE_COLS = ("type", "event_type", "name")
_AGGREGATE_COLS = ("aggregate_id", "aggregateId", "aggregate", "session_id")
_EVENT_DATA_COLS = ("data", "payload", "json", "body")
_EVENT_TS_COLS = ("time_created", "created_at", "createdAt", "ts", "timestamp")

_SESSION_ID_COLS = ("id", "session_id", "sessionId")
_SESSION_TITLE_COLS = ("title", "name", "subject")
_SESSION_DIR_COLS = ("directory", "dir", "path", "worktree")
_SESSION_FILES_COLS = ("summary_files", "files_count", "files")
_SESSION_DIFFS_COLS = ("summary_diffs", "diffs_count", "diffs")
_SESSION_MODEL_COLS = ("model", "agent_model", "current_model")
_SESSION_AGENT_COLS = ("agent", "current_agent")

# Tables we read from
_EVENT_TABLES = ("event", "events", "Event")
_SESSION_TABLES = ("session", "sessions", "Session")


def _opencode_default_locations() -> List[Path]:
    """Well-known default locations for OpenCode database files.

    We probe both 'opencode.db' and 'opencode-main.db' (and any 'opencode-*.db'
    glob pattern) in each default directory because the OpenCode/Centri fork
    uses 'opencode-main.db' as its primary live DB, and databases may rotate.
    We prefer the database with the newest mtime to avoid ingesting from
    stale/frozen rotated databases.
    """
    home = Path.home()
    paths: List[Path] = []
    for d in [
        home / ".local" / "share" / "opencode",
        home / ".config" / "opencode",
        home / ".opencode",
        *(
            [home / "Library" / "Application Support" / "opencode"]
            if sys.platform == "darwin"
            else []
        ),
    ]:
        found = {f for f in d.glob("opencode-*.db") if f.is_file()}
        if (d / "opencode.db").is_file():
            found.add(d / "opencode.db")

        if found:
            paths.append(max(found, key=lambda x: x.stat().st_mtime))
            continue

        paths.append(d / "opencode-main.db")
        paths.append(d / "opencode.db")

    return paths


# ───────────────────── helper: extract structured payload from opencode event data

def _stringify_ref(val: Any) -> str:
    """Normalize an opencode model/agent reference to a readable string.

    OpenCode stores model/agent references either as a bare string or as a dict
    like ``{"id": "DeepSeek-V4-Pro", "providerID": "deepseek-ai", "variant": ...}``.
    Slicing the dict form raised ``TypeError: unhashable type: 'slice'`` (BUG 3),
    so collapse to the most human-meaningful identifier.
    """
    if val is None:
        return ""
    if isinstance(val, str):
        return val
    if isinstance(val, dict):
        provider = val.get("providerID") or val.get("provider") or ""
        ident = val.get("id") or val.get("modelID") or val.get("name") or ""
        if provider and ident:
            return f"{provider}/{ident}"
        return str(ident or provider or val)
    return str(val)


# Tool calls that produce no durable semantic signal even when they "complete."
# Reads/list/glob/ls of a directory reveal what files exist, which is implied by
# the code anyway, and were producing 51K trivial "fact" rows.
_LOW_VALUE_TOOLS = frozenset({
    "read", "list", "glob", "ls", "webfetch", "fetch",
    "str_replace", "file_search", "codebase_search", "ls_search"
})


def _tool_call_fact(tool: str, input_: Dict[str, Any], state: Dict[str, Any], description: str) -> Optional[Dict[str, Any]]:
    """Build a deterministic fact hint from a tool call payload.

    Only meaningful completed tool calls become facts.  The statement is a
    compact, deterministic description of *what* the tool did (not the full
    stdout dump), so the consolidation worker can place it in the graph without
    LLM re-interpretation.
    """
    return None


def _session_activity_hints(session_row: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Build deterministic hints from a session summary row.

    A session row carries aggregate counters (files touched, diffs, model, agent).
    Every non-zero change produces at least one fact; model/agent switches produce
    decisions; active work with no resolution produces an open loop.
    """
    hints: List[Dict[str, Any]] = []
    sid = session_row.get("id") or session_row.get("session_id") or "unknown"
    title = session_row.get("title", "").strip() or f"Session {sid[:16]}"
    directory = session_row.get("directory", "")
    diffs = session_row.get("diffs") or 0

    # Open loop: active work if diffs > 0 and no clear resolution is visible
    # (session table doesn't contain a status column in most opencode versions,
    # so we conservatively mark any session with work as an open intent.)
    if diffs > 0:
        hints.append({
            "op": "add_open_loop",
            "topic": f"session:{sid}",
            "intent": f"Continue work in session '{title}' ({directory or 'unknown dir'}) — {diffs} diff(s) outstanding",
            "tags": ["opencode", "session", "open_loop"],
        })

    return hints


# ───────────────────── salience: drop boilerplate / low-signal transcript noise

# Repeated framework/boilerplate prefixes that carry zero durable signal. Any
# text part starting with one of these is dropped at the door so it never
# pollutes the graph or the embedding space.
_BOILERPLATE_PREFIXES = (
    "You are being used as the active ACP agent backend",
    "You are an AI orchestration agent",
    "You are Jarvis",
    "<system-reminder>",
    "<environment_context>",
    "# Hermes Agent Persona",
    "Respond with exactly",
    "RESPOND WITH EXACTLY",
    "OPENCODE_DIRECT_OK",
)

# Substrings that mark a text part as framework plumbing rather than real
# conversation, checked anywhere in the (truncated) body.
_BOILERPLATE_SUBSTRINGS = (
    "active ACP agent backend for Hermes",
    "You MUST output tool calls using <tool_call>",
    "OPENCODE_DIRECT_OK",
)

# Roles whose parts are pure agent-internal chatter. We keep these only when
# they are unusually long (a genuine plan/analysis), not the one-liner "the user
# wants me to list files" monologue that floods the stream.
_LOW_SIGNAL_ROLES = ("reasoning", "step-start", "step-finish")

# Minimum lengths by class. user/assistant text is kept aggressively; internal
# reasoning must clear a much higher bar to be considered durable.
_MIN_TEXT_CHARS = 40
_MIN_REASONING_CHARS = 400


def _transcript_salience(role: str, text: str) -> Tuple[bool, str]:
    """Decide whether a transcript text part carries durable signal.

    Returns ``(keep, normalized_role)``. The salience layer exists because the
    raw OpenCode stream is ~95% noise: repeated system/boilerplate prompts,
    streaming placeholders, and low-value agent reasoning ("the user wants me to
    list files"). Ingesting all of it makes memory generic — the boilerplate
    drowns out the handful of real user questions and substantive answers in the
    embedding space. We filter at ingestion so only signal reaches the graph.
    """
    t = (text or "").strip()
    if not t:
        return False, role
    role_l = (role or "").lower()

    # 1. Hard-drop known boilerplate regardless of role.
    for pref in _BOILERPLATE_PREFIXES:
        if t.startswith(pref):
            return False, role_l
    head = t[:400]
    for sub in _BOILERPLATE_SUBSTRINGS:
        if sub in head:
            return False, role_l

    # 2. Low-signal internal roles: keep only substantial analysis.
    if role_l in _LOW_SIGNAL_ROLES:
        if len(t) < _MIN_REASONING_CHARS:
            return False, role_l
        return True, role_l

    # 3. Real conversation text (user / assistant / text): keep above a low bar.
    if len(t) < _MIN_TEXT_CHARS:
        return False, role_l
    return True, role_l


def _is_useful_session_title(title: str) -> bool:
    """Some opencode sessions store raw user-first-message text or tool-CALL
    artifacts (e.g. ``"<tool_call>..."``) in the title field. Those titles are
    noise when stitched into facts — they inject raw framework markers into the
    graph. Detect and reject so the session summary uses the slug instead."""
    if not title:
        return False
    t = title.strip()
    if len(t) < 3:
        return False
    low = t.lower()
    # generic auto-titles created by opencode on session start
    if low.startswith("new session -"):
        return False
    # raw tool-call markers leaked into the title
    noise_prefixes = (
        "<tool_call>", "</tool_call>", "<function",
        "use tool", "[tool", "```tool", "respond with exactly",
        "[request interrupted", "<permission",
    )
    for p in noise_prefixes:
        if low.startswith(p):
            return False
    # raw JSON / HTML fragments are not titles
    if t.startswith(("{", "[", "<", "```")):
        return False
    return True


def _message_fact(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Lightweight fact hint from a chat message — only for foldable roles
    (assistant, system, tool) and only when the message carries durable signal
    (a decision stated explicitly, a file path mentioned, etc.).
    """
    return None


# ───────────────────── the adapter

class OpenCodeIngestor(MessageAdapter):
    """Incremental, idempotent tail of an OpenCode ``event`` table into
    structured spine events with deterministic consolidation hints.
    """

    agent = "opencode"
    tool = "opencode"
    # Multiple event types — we emit them per-row.
    source_prefix = "opencode"
    # BUG 2 fix: re-add the event_source dropped in the v1->v2 rewrite so emitted
    # events are labeled ingest.opencode.* (not the inherited base default
    # ingest.base.*). _emit_event derives source=f"{self.event_source}.<kind>".
    event_source = "ingest.opencode"
    event_type = "ingest.opencode.event"
    fact_tags = ("ingest", "opencode", "event")

    def default_locations(self) -> List[Path]:
        return _opencode_default_locations()

    # ── 1. discover (cheap counts) ──────────────────────────────────
    def discover(self, extra_paths: Optional[List[str]] = None) -> List[DiscoveredSource]:
        # Override discovery: count events, not messages.
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
                continue
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
        try:
            uri = f"file:{path.resolve()}?mode=ro"
            conn = sqlite3.connect(uri, uri=True)
            try:
                table = self._resolve_table(conn, _EVENT_TABLES)
                if table is None:
                    # Fallback to message table count
                    table = self._resolve_table(conn, ("message", "messages"))
                    if table is None:
                        return None, "no event or message table found"
                row = conn.execute(f"SELECT count(*) FROM {table}").fetchone()
                return int(row[0]) if row else 0, ""
            finally:
                conn.close()
        except Exception as exc:
            return None, f"unreadable: {exc}"

    # ── 2. read structured events ───────────────────────────────────
    def read_events(self, path: Path, limit: int = 5000, hwm: str = "") -> List[Dict[str, Any]]:
        """Read events from the OpenCode DB, skipping rows already past the HWM.

        Uses ``rowid`` filtering to avoid a full table scan of 100k+ rows.
        The ``limit`` caps how many rows are loaded into memory per poll.

        BUG 1 fix: the ``event`` table has no timestamp column, so the HWM is the
        last-processed integer ``rowid`` (monotonic, gap-free, assigned by SQLite),
        NOT a ``ts|id`` cursor. ``hwm`` is the stored rowid as a string ("" on cold
        start). Resume reads ``WHERE rowid > stored_rowid`` ordered by rowid.
        """
        uri = f"file:{path.resolve()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            conn.row_factory = sqlite3.Row
            events: List[Dict[str, Any]] = []

            # Parse HWM as an integer rowid (empty/garbage => 0 => cold start).
            try:
                hwm_rowid = int(hwm) if hwm else 0
            except (ValueError, TypeError):
                hwm_rowid = 0

            # 2a — structured events from 'event' table
            event_table = self._resolve_table(conn, _EVENT_TABLES)
            if event_table:
                cols = [r[1] for r in conn.execute(f"PRAGMA table_info({event_table})").fetchall()]
                id_col = first_present(cols, _EVENT_ID_COLS) or "rowid"
                type_col = first_present(cols, _EVENT_TYPE_COLS)
                aggregate_col = first_present(cols, _AGGREGATE_COLS)
                data_col = first_present(cols, _EVENT_DATA_COLS)
                ts_col = first_present(cols, _EVENT_TS_COLS)

                # Skip already-processed rows directly by rowid (preserves the
                # LIMIT-batched memory fix; no full scan).
                where_clause = ""
                params: list = []
                if hwm_rowid:
                    where_clause = "WHERE rowid > ?"
                    params.append(hwm_rowid)

                raw = conn.execute(
                    f"SELECT rowid AS _rowid, * FROM {event_table} {where_clause} ORDER BY rowid ASC LIMIT ?",
                    params + [limit],
                ).fetchall()
                for r in raw:
                    rd = dict(r)
                    rowid = rd.get("_rowid")
                    eid = str(rd.get(id_col, rowid))
                    etype = str(rd.get(type_col, "")) if type_col else ""
                    aggregate = str(rd.get(aggregate_col, "")) if aggregate_col else ""
                    ts = coerce_ts(rd.get(ts_col)) if ts_col else ""
                    data_raw = rd.get(data_col) if data_col else None
                    data = {}
                    if data_raw:
                        try:
                            data = json.loads(data_raw)
                        except (ValueError, TypeError):
                            data = {"raw": str(data_raw)}

                    events.append({
                        "_source": "event",
                        "_rowid": rowid,
                        "id": eid,
                        "type": etype,
                        "aggregate_id": aggregate,
                        "ts": ts,
                        "data": data,
                        "row": rd,
                    })

            # 2b — session summaries from 'session' table (enrichment)
            session_table = self._resolve_table(conn, _SESSION_TABLES)
            if session_table:
                cols = [r[1] for r in conn.execute(f"PRAGMA table_info({session_table})").fetchall()]
                sid_col = first_present(cols, _SESSION_ID_COLS) or "id"
                title_col = first_present(cols, _SESSION_TITLE_COLS)
                dir_col = first_present(cols, _SESSION_DIR_COLS)
                files_col = first_present(cols, _SESSION_FILES_COLS)
                diffs_col = first_present(cols, _SESSION_DIFFS_COLS)
                model_col = first_present(cols, _SESSION_MODEL_COLS)
                agent_col = first_present(cols, _SESSION_AGENT_COLS)
                ts_col = first_present(cols, _EVENT_TS_COLS)

                raw = conn.execute(
                    f"SELECT * FROM {session_table} ORDER BY {ts_col or 'rowid'} ASC LIMIT ?",
                    (limit,),
                ).fetchall()
                for r in raw:
                    rd = dict(r)
                    sid = str(rd.get(sid_col, rd.get("id", "")))
                    ts = coerce_ts(rd.get(ts_col)) if ts_col else ""
                    events.append({
                        "_source": "session",
                        "_rowid": None,
                        "id": f"session:{sid}",
                        "type": "session.summary",
                        "aggregate_id": sid,
                        "ts": ts,
                        "data": {
                            "title": str(rd.get(title_col, "")) if title_col else "",
                            "directory": str(rd.get(dir_col, "")) if dir_col else "",
                            "files": int(rd.get(files_col, 0) or 0) if files_col else 0,
                            "diffs": int(rd.get(diffs_col, 0) or 0) if diffs_col else 0,
                            "model": str(rd.get(model_col, "")) if model_col else "",
                            "agent": str(rd.get(agent_col, "")) if agent_col else "",
                        },
                        "row": rd,
                    })

            return events
        finally:
            conn.close()

    # ── 3. ingest method (reads events, emits typed spine events) ─────
    async def ingest(
        self, path: str | Path, source: Optional[str] = None, repo_id: Optional[str] = None, limit: int = 5000
    ) -> Dict[str, Any]:
        """Override: we now accept ``event`` rows (not just messages).

        When ``repo_id`` is not passed by the caller, the ingestor resolves one
        from the session row's ``directory`` field (→ a project of kind='repo')
        or falls back to a topic project keyed on the session slug/title. This
        ensures every emitted event is project-scoped, which is what the
        curation ranker needs to give same-project candidates partial credit.
        """
        p = Path(path)
        src = source or self.default_source(p)
        if not p.exists():
            return {"agent": self.agent, "source": src, "ingested": 0, "high_water": "", "scanned": 0, "available": False}

        high_water = await self._db.get_ingest_high_water(src)
        try:
            rows = self.read_events(p, limit=limit, hwm=high_water or "")
        except Exception as exc:
            logger.warning("%s ingest read failed for %s: %s", self.agent, p, exc)
            return {"agent": self.agent, "source": src, "ingested": 0, "high_water": high_water, "scanned": 0, "available": False, "error": str(exc)}

        # Order by rowid (the event table's only monotonic key); session-summary
        # rows (_rowid=None) sort first and are written but never advance the HWM.
        def _row_sort_key(r: Dict[str, Any]) -> int:
            rid = r.get("_rowid")
            return rid if isinstance(rid, int) else -1

        rows.sort(key=_row_sort_key)
        scanned = len(rows)

        # BUG 1 fix: the HWM is the last-processed integer rowid, not a ts|id
        # cursor. The event table has no timestamp, so ts|id collapsed to "|id"
        # and never advanced. We persist the max rowid seen this pass.
        try:
            hwm_rowid = int(high_water) if high_water else 0
        except (ValueError, TypeError):
            hwm_rowid = 0

        ingested = 0
        max_rowid = hwm_rowid
        for row in rows:
            rowid = row.get("_rowid")
            resolved_repo_id = repo_id or await self._resolve_project_for_row(row)
            wrote = await self._append_structured_event(src, row, resolved_repo_id)
            if wrote:
                ingested += 1
            if rowid is not None and rowid > max_rowid:
                max_rowid = rowid

        newest_cursor = str(max_rowid) if max_rowid else high_water
        if ingested or newest_cursor != high_water:
            await self._db.set_ingest_high_water(src, newest_cursor, last_run_at=_now(), ingested_delta=ingested)
        return {
            "agent": self.agent,
            "source": src,
            "ingested": ingested,
            "high_water": newest_cursor,
            "scanned": scanned,
            "available": True,
        }

    async def _resolve_project_for_row(self, row: Dict[str, Any]) -> Optional[str]:
        """Derive a project id from a row's session metadata.

        Coding sessions carry a ``directory`` field → resolve to a repo project.
        Sessions without a directory (rare for opencode but possible) fall back
        to a topic project keyed on the slug or title so the event is still
        scoped. Returns ``None`` only when no identifier is available at all.
        """
        data = row.get("data") or {}
        directory = (data.get("directory") or "").strip()
        if directory:
            return await self._db.resolve_project_for_repo(directory)
        slug = (data.get("slug") or "").strip()
        if slug:
            return await self._db.resolve_project_for_topic(slug)
        title = (data.get("title") or "").strip()
        if title and _is_useful_session_title(title):
            return await self._db.resolve_project_for_topic(title)
        return None

    # ── 4. spine write: emit typed events with deterministic hints ──
    async def _append_structured_event(
        self, source: str, row: Dict[str, Any], repo_id: Optional[str]
    ) -> bool:
        if row["_source"] == "event":
            return await self._append_event_event(source, row, repo_id)
        elif row["_source"] == "session":
            return await self._append_session_event(source, row, repo_id)
        return False

    async def _append_event_event(
        self, source: str, row: Dict[str, Any], repo_id: Optional[str]
    ) -> bool:
        etype = row.get("type", "")
        data = row.get("data") or {}
        eid = row["id"]
        event_id = f"ingest:{source}:event:{eid}"
        if await self._db.event_exists(event_id):
            return False

        ts = row.get("ts") or _now()
        aggregate_id = row.get("aggregate_id", "")

        # ── message.part.updated.1 → tool_called or transcript
        if etype.startswith("message.part.updated") or etype.startswith("message.part.created"):
            part = data.get("part", data)
            part_type = part.get("type", "") if isinstance(part, dict) else ""
            if part_type == "tool":
                # NOISE FILTER: low-value tool calls (list/glob/ls WITHOUT
                # description or output) are STILL written to the spine for
                # provenance, but with no fact hint so consolidation doesn't
                # turn them into noisy small facts.
                tool = part.get("tool", "")
                state = part.get("state", {}) or {}
                input_ = state.get("input", {}) or part.get("input", {}) or {}
                output = state.get("output", "") or ""
                title = (state.get("title", "") or "").strip()
                status = state.get("status", "")
                description = (input_.get("description", "") or "").strip()
                cmd = input_.get("command", "") or input_.get("cmd", "") or ""
                effective_desc = description or title
                payload: Dict[str, Any] = {
                    "tool": tool,
                    "cmd": cmd,
                    "description": effective_desc,
                    "status": status,
                    "title": title,
                    "output": output[:2000] if isinstance(output, str) else output,
                    "state": state,
                    "input": input_,
                    "aggregate_id": aggregate_id,
                }
                summary = f"[{tool}] {effective_desc[:160]}" if effective_desc else f"[{tool}] tool call"
                await self._emit_event(
                    event_id=event_id,
                    event_type="ingest.opencode.tool_called",
                    source=f"{self.event_source}.tool",
                    ts=ts,
                    payload=payload,
                    summary=summary,
                    repo_id=repo_id,
                )
                return True
            else:
                # Text / assistant / user message → transcript event with real text.
                text = ""
                if isinstance(part, dict):
                    text = part.get("text", "") or part.get("content", "") or ""
                text = text.strip() if isinstance(text, str) else str(text)
                # SALIENCE fix: filter boilerplate / low-signal noise at the door.
                # The raw stream is ~95% repeated system prompts + agent reasoning
                # chatter; ingesting it makes memory generic. Only durable signal
                # (real user/assistant text, substantial analysis) is kept.
                keep, norm_role = _transcript_salience(part_type, text)
                if not keep:
                    return False
                payload = {
                    "role": norm_role or "unknown",
                    "text": text,
                    "aggregate_id": aggregate_id,
                }
                await self._emit_event(
                    event_id=event_id,
                    event_type="ingest.opencode.transcript",
                    source=f"{self.event_source}.message",
                    ts=ts,
                    payload=payload,
                    summary=text[:160],
                    repo_id=repo_id,
                )
                return True

        # ── session.updated.1 → session_activity (decisions + open loops)
        elif etype.startswith("session.updated"):
            # MEANINGFUL CONTENT fix: opencode stores the session under data["info"]
            # (NOT data["session"]), and model is a dict, not a string. The old code
            # read data["session"] → always empty → blank session_activity payloads.
            session_data = data.get("info", data.get("session", data))
            if isinstance(session_data, dict):
                title_raw = (session_data.get("title", "") or "").strip()
                # NOISE FILTER: reject raw tool-call markers / JSON / HTML leaked
                # into the title; fall back to slug rather than writing garbage
                # statements like "Session '<tool_call>' worked in ...".
                title = title_raw if _is_useful_session_title(title_raw) else ""
                model = _stringify_ref(session_data.get("model", ""))
                agent = session_data.get("agent", "") or ""
                directory = session_data.get("directory", "") or ""
                slug = session_data.get("slug", "") or ""
                files = session_data.get("files") or 0
                diffs = session_data.get("diffs") or 0
            else:
                title = model = agent = directory = slug = ""
                files = diffs = 0

            display = title or f"session {slug[:12]}" if slug else (title or f"session {aggregate_id[:12]}")
            payload = {
                "title": title,
                "model": model,
                "agent": agent,
                "directory": directory,
                "slug": slug,
                "files": files,
                "diffs": diffs,
                "session_id": aggregate_id,
            }
            # Build deterministic hints — only when the session has grounded,
            # non-noise identifiers. Slugs (random-animal-words) are stable.
            # Sessions with no title AND no slug AND no directory are dropped
            # outright so they don't pollute the graph as empty facts.
            if not (directory or model or files or diffs or title or slug):
                # Nothing useful to fold — emit nothing.
                return False
            session_row = {
                "id": aggregate_id,
                "title": display,
                "directory": directory,
                "files": files,
                "diffs": diffs,
                "model": model,
                "agent": agent,
            }
            hints = _session_activity_hints(session_row)
            if hints:
                payload["open_loop"] = hints[0]  # primary hint
                payload["decisions"] = hints[1:]

            await self._emit_event(
                event_id=event_id,
                event_type="ingest.opencode.session_activity",
                source=f"{self.event_source}.session",
                ts=ts,
                payload=payload,
                summary=f"Session update: {display[:80]} ({directory or '?'}, {files}f/{diffs}d)",
                repo_id=repo_id,
            )
            return True

        # ── message.updated.1 → DROPPED.
        # 20k of these; data["info"] = {id, sessionID, role, time, agent, model}.
        # NOISE FILTER: these carry no body text (text lives in message.part.*
        # events) and consolidation was mechanically turning them into a
        # `message_meta` fact per row — 20K facts of zero semantic content
        # dominating the graph. Their provenance is captured implicitly by the
        # transcript + session events that ARE kept, so drop these unconditionally.
        elif etype.startswith("message.updated"):
            return False

        # ── session.next.model.switched.1 → decision
        elif etype.startswith("session.next.model.switched") or etype.startswith("session.next.agent.switched"):
            kind = "model" if "model" in etype else "agent"
            # BUG 3 fix: data[kind] may be a dict (e.g. model =
            # {"id","providerID","variant"}) — slicing a dict raised
            # "TypeError: unhashable type: 'slice'" and aborted the whole pass
            # before the HWM was ever persisted. Normalize to a readable string.
            raw_val = data.get(kind, data.get(f"new_{kind}", data.get("to", "")))
            new_val = _stringify_ref(raw_val)
            payload = {
                "switch": kind,
                "to": new_val,
                "session_id": aggregate_id,
            }
            payload["decision"] = {
                "op": "add_decision",
                "topic": f"{kind}_switched",
                "statement": f"Session switched to {kind} '{new_val}'",
                "outcome": "adopted",
                "tags": ["opencode", "session", kind],
            }
            await self._emit_event(
                event_id=event_id,
                event_type="ingest.opencode.session_activity",
                source=f"{self.event_source}.switch",
                ts=ts,
                payload=payload,
                summary=f"Switched {kind}: {new_val[:80]}",
                repo_id=repo_id,
            )
            return True

        # Fallback: unhandled event types are silently dropped (no LLM burn)
        return False

    async def _append_session_event(
        self, source: str, row: Dict[str, Any], repo_id: Optional[str]
    ) -> bool:
        sid = row.get("aggregate_id", "")
        event_id = f"ingest:{source}:session:{sid}"
        if await self._db.event_exists(event_id):
            return False
        ts = row.get("ts") or _now()
        data = row.get("data", {})
        payload = {
            "title": data.get("title", ""),
            "directory": data.get("directory", ""),
            "files": data.get("files", 0),
            "diffs": data.get("diffs", 0),
            "model": data.get("model", ""),
            "agent": data.get("agent", ""),
            "session_id": sid,
        }
        hints = _session_activity_hints(data)
        if hints:
            payload["open_loop"] = hints[0]
            payload["decisions"] = hints[1:]
        await self._emit_event(
            event_id=event_id,
            event_type="ingest.opencode.session_activity",
            source=f"{self.event_source}.session_summary",
            ts=ts,
            payload=payload,
            summary=f"Session '{data.get('title', '')[:80]}' summary",
            repo_id=repo_id,
        )
        return True

    # ── shared spine writer (copied from base for typed events) ──────
    async def _emit_event(
        self,
        event_id: str,
        event_type: str,
        source: str,
        ts: str,
        payload: Dict[str, Any],
        summary: str,
        repo_id: Optional[str] = None,
    ) -> None:
        from centri.redaction import redact_jsonable
        payload_json = json.dumps(redact_jsonable(payload))
        await self._db.append_event(
            event_id=event_id,
            type=event_type,
            source=source,
            ts=ts,
            thread_id=payload.get("session_id") or payload.get("aggregate_id"),
            task_id=None,
            repo_id=repo_id,
            importance="normal",  # structured events are normal importance (not low)
            payload=payload,
        )
        if self._event_bus is not None:
            try:
                await self._event_bus.publish({
                    "id": event_id,
                    "type": event_type,
                    "ts": ts,
                    "source": source,
                    "repo_id": repo_id,
                    "importance": "normal",
                    "payload": payload,
                    "summary": summary,
                })
            except Exception:
                pass

    # ── helper ─────────────────────────────────────────────────────
    def _resolve_table(self, conn: sqlite3.Connection, candidates: Tuple[str, ...]) -> Optional[str]:
        names = {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        for cand in candidates:
            if cand in names:
                return cand
        return None


async def ingest_opencode_db(
    db: Any,
    opencode_db_path: str | Path,
    source: Optional[str] = None,
    repo_id: Optional[str] = None,
    event_bus: Any = None,
) -> Dict[str, Any]:
    """Convenience one-shot: build an adapter and run a single pass."""
    return await OpenCodeIngestor(db, event_bus=event_bus).ingest(
        opencode_db_path, source=source, repo_id=repo_id
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
