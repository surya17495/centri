"""Prompt + proposal-op contract for the LLM consolidation tier (Increment 3).

The deterministic hint path (``Consolidator.consume_events``) stays the
authoritative production path. This module backs the *second* tier: events that
carry **no** synthesis hint (raw stdout, transcripts) are batched and handed to a
background "memory worker" LLM that may only **propose** typed operations. The
LLM never writes the graph; it emits a JSON array of ops which a deterministic
gatekeeper (in :mod:`centri.consolidation`) validates and applies or rejects.

Design lineage: this adapts Letta's sleep-time memory lessons to CENTRI's typed
graph — a background worker persona, "be selective but aim for high recall",
**absolute dates only** (the relative-time lesson: "today"/"recently" rot), and
**small precise ops over rewrites** (supersede a single node, do not restate the
world). The whole spine is never sent; only the unhinted batch plus a budgeted
digest of current live nodes so the model can dedupe and supersede correctly.

Everything here is pure string/JSON shaping — no network, no wall-clock — so the
prompt builder and the op parser are deterministically testable offline.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Op schema — the proposal contract
# ---------------------------------------------------------------------------
# Every op the model may emit. The gatekeeper validates each against this table
# (required keys present, no unknown op) before any graph touch. Optional keys
# are listed separately so a missing optional is fine but an unknown key on a
# known op is tolerated-and-ignored (forward-compatible) while a missing required
# key is a hard reject.
OP_ADD_FACT = "add_fact"
OP_ADD_DECISION = "add_decision"
OP_OPEN_LOOP = "open_loop"
OP_CLOSE_LOOP = "close_loop"
OP_SUPERSEDE = "supersede"
OP_PROFILE_UPDATE = "profile_update"
OP_FINISH = "finish"

# kind -> (required keys, optional keys)
OP_SCHEMA: Dict[str, Tuple[Tuple[str, ...], Tuple[str, ...]]] = {
    OP_ADD_FACT: (("topic", "statement"), ("tags",)),
    OP_ADD_DECISION: (("topic", "statement"), ("stance", "rationale", "tags")),
    OP_OPEN_LOOP: (("intent",), ("cue", "tags")),
    # close_loop matches by explicit loop id OR by intent text — exactly one is
    # required; the gatekeeper enforces the either/or.
    OP_CLOSE_LOOP: ((), ("loop_id", "intent_match", "resolution")),
    OP_SUPERSEDE: (("node_id", "kind", "new_statement"), ("topic",)),
    OP_PROFILE_UPDATE: (("key", "value"), ()),
    OP_FINISH: ((), ()),
}

ALL_OPS = tuple(OP_SCHEMA.keys())


def op_schema_summary() -> List[Dict[str, Any]]:
    """Machine/doc-friendly summary of the op contract (used by docs + tests)."""
    out: List[Dict[str, Any]] = []
    for op in ALL_OPS:
        required, optional = OP_SCHEMA[op]
        out.append({"op": op, "required": list(required), "optional": list(optional)})
    return out


# ---------------------------------------------------------------------------
# Live-node digest (budgeted) — so the model can dedupe / supersede correctly
# ---------------------------------------------------------------------------
@dataclass
class LiveDigest:
    """A compact, budgeted view of the current live graph for the prompt.

    Carries node ids so the model can target a ``supersede`` precisely. Pure data
    — :func:`build_live_digest` fills it from graph reads in the consolidator.
    """

    decisions: List[Dict[str, str]] = field(default_factory=list)
    facts: List[Dict[str, str]] = field(default_factory=list)
    open_loops: List[Dict[str, str]] = field(default_factory=list)

    def render(self) -> str:
        if not (self.decisions or self.facts or self.open_loops):
            return "(no current memory nodes)"
        lines: List[str] = []
        if self.decisions:
            lines.append("DECISIONS (live):")
            for d in self.decisions:
                lines.append(f"  [{d['id']}] ({d.get('stance', 'adopted')}) {d['topic']}: {d['statement']}")
        if self.facts:
            lines.append("FACTS (live):")
            for f in self.facts:
                lines.append(f"  [{f['id']}] {f['topic']}: {f['statement']}")
        if self.open_loops:
            lines.append("OPEN LOOPS (live):")
            for loop in self.open_loops:
                lines.append(f"  [{loop['id']}] {loop['intent']}")
        return "\n".join(lines)


def _truncate(text: str, limit: int) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ---------------------------------------------------------------------------
# Batch rendering
# ---------------------------------------------------------------------------
def render_batch(events: List[Dict[str, Any]], *, per_event_chars: int = 600) -> str:
    """Render the unhinted event batch the model reasons over.

    Each line is ``<event_id> [<type>] <text>`` — the event id is shown so the
    gatekeeper can attribute applied ops back to their source events (provenance).
    Text comes from the payload's most informative free-text field; long stdout
    is truncated per event to keep the prompt O(batch), not O(spine).
    """
    lines: List[str] = []
    for ev in events:
        ts = ev.get("ts") or ev.get("timestamp") or "unknown-time"
        ts_str = str(ts)
        if len(ts_str) > 19:
            ts_str = ts_str[:19]
        etype = ev.get("type") or "event"
        text = _event_text(ev.get("payload") or {})
        lines.append(f"{ts_str} [{etype}] {_truncate(text, per_event_chars)}")
    return "\n".join(lines) if lines else "(empty batch)"


def _event_text(payload: Dict[str, Any]) -> str:
    """Pull the most informative free text from an unhinted event payload."""
    for key in ("text", "message", "stdout", "output", "summary", "content", "line"):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return val
    # Fall back to a compact JSON of scalar fields so nothing is silently empty.
    scalars = {k: v for k, v in payload.items() if isinstance(v, (str, int, float, bool))}
    return json.dumps(scalars, sort_keys=True) if scalars else ""


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = (
    "You are CENTRI's background memory worker. You run during a 'sleep cycle' to "
    "fold raw activity (terminal output, transcripts) into a small, typed,"
    " long-lived memory graph of a single builder's project.\n\n"
    "You DO NOT write memory directly. You PROPOSE operations as a single JSON "
    "array; a deterministic gatekeeper validates and applies them.\n\n"
    "## What to capture vs what to ignore\n\n"
    "CAPTURE (durable knowledge that will be true tomorrow):\n"
    "- Architectural decisions and their rationale ('we chose X because Y')\n"
    "- Conventions and patterns established ('namespace X for events')\n"
    "- Root cause findings ('the crash was caused by Z')\n"
    "- Completed work with verifiable outcomes ('backtest V5 ran on dataset D, result: +3912% PnL')\n"
    "- User preferences and work habits (emit as profile_update)\n"
    "- Project state changes ('Centri V2 migration complete')\n"
    "- Ongoing work that is NOT finished (emit as open_loop)\n\n"
    "IGNORE (transient, will be stale immediately):\n"
    "- What tool was used ('the user used the read tool') — NOT a fact\n"
    "- Current status of running processes ('transfer still running') — ephemeral\n"
    "- Momentary observations ('scan timer says 16m ago') — meaningless later\n"
    "- Greetings, acknowledgments, filler\n"
    "- Individual commands without context ('ran ls -la')\n"
    "- Raw output that doesn't establish anything durable\n\n"
    "## Topic consistency\n\n"
    "Topics are the graph's primary key. Reuse existing topics from CURRENT MEMORY "
    "when the new fact is about the same subject. Do NOT create a new topic for "
    "every minor variation. If a fact about 'futures-agent-backtest' exists and "
    "you have a new backtest result, use topic 'futures-agent-backtest' — not "
    "'futures-agent-backtest-v5' or 'backtest results june 17'.\n\n"
    "Good topics: 'futures-agent-model-eval', 'centri-memory-architecture', "
    "'feed-handler-stability'\n"
    "Bad topics: 'wrapper test execution', 'scan timer', 'tool usage', "
    "'code modification', 'transfer status'\n\n"
    "## Open loops\n\n"
    "When the user mentions work that is in progress or planned but not complete, "
    "emit an open_loop. Examples: 'porting adaptive-limit entry to live', "
    "'investigating feed-handler silent death', 'model retraining on new dataset'. "
    "If an open loop for the same work already exists, do NOT re-create it. "
    "Only close a loop when the work is confirmed done.\n\n"
    "## Narrative extraction\n\n"
    "When you see a sequence of events showing a problem being investigated and "
    "solved, capture the FULL arc: what was the problem, what was the root cause, "
    "what was the fix, and how it was verified. Do not extract isolated facts from "
    "individual events — connect related events into a coherent statement. "
    "Example: instead of 'the user ran a command' + 'output showed error X' + "
    "'user edited file Y', extract: 'feed-handler crash root cause was missing "
    "error handling in WS reconnect; fixed by adding retry logic in "
    "feed-handler.ts:142; verified by 24h stable run'.\n\n"
    "## Obsolescence detection\n\n"
    "Examine CURRENT MEMORY alongside new activity. If any existing node is now "
    "obsolete — superseded by newer decisions, contradicted by newer facts, or the "
    "project/system it references has been abandoned — emit a `supersede` or "
    "`close_loop` targeting its node_id.\n\n"
    "## Rules\n\n"
    "- ABSOLUTE DATES ONLY. Never write 'today', 'yesterday', 'recently', 'now'. "
    "Use the event's timestamp (YYYY-MM-DD). A relative-time statement will be REJECTED.\n"
    "- Dedupe against CURRENT MEMORY: if a fact already exists, do not re-add it; "
    "supersede only if the new information actually changes it.\n"
    "- Every statement must be self-contained — readable without the raw log.\n"
    "- Prefer SMALL PRECISE ops. To update, supersede by node_id, don't restate.\n"
    "- Propose profile_update for user preferences, work habits, project context.\n"
    "- If nothing is worth remembering, return [{\"op\":\"finish\"}].\n\n"
    "Allowed operations (emit a JSON array of these objects, nothing else):\n"
    '  {"op":"add_fact","topic":str,"statement":str,"tags"?:[str]}\n'
    '  {"op":"add_decision","topic":str,"statement":str,"stance"?:"adopted"|"rejected","rationale"?:str}\n'
    '  {"op":"open_loop","intent":str,"cue"?:str}\n'
    '  {"op":"close_loop","loop_id"?:str,"intent_match"?:str,"resolution"?:"done"|"parked"}\n'
    '  {"op":"supersede","node_id":str,"kind":"fact"|"decision","new_statement":str}\n'
    '  {"op":"profile_update","key":str,"value":str}\n'
    '  {"op":"finish"}\n\n'
    "Emit your ops as one JSON array, then a final {\"op\":\"finish\"}."
)


def build_messages(
    batch_events: List[Dict[str, Any]],
    digest: LiveDigest,
    *,
    per_event_chars: int = 600,
) -> List[Dict[str, str]]:
    """Assemble the OpenAI-style chat messages for one consolidation batch.

    Token discipline: the prompt is the system contract + the budgeted live-node
    digest + the unhinted batch only — never the whole spine.
    """
    user = (
        "CURRENT MEMORY (dedupe / supersede against these; ids in brackets):\n"
        f"{digest.render()}\n\n"
        "NEW UNPROCESSED ACTIVITY (each line: <event_id> [<type>] <text>):\n"
        f"{render_batch(batch_events, per_event_chars=per_event_chars)}\n\n"
        "Propose the memory operations. Return ONLY a JSON array of ops ending "
        "with a finish op."
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------
def parse_ops(content: Optional[str]) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Parse the model's reply into a list of op dicts.

    Returns ``(ops, error)``. ``error`` is non-None when the reply is not a JSON
    array of objects (malformed) — the gatekeeper turns that into a single
    rejection receipt and leaves the graph untouched. Tolerates a JSON array
    fenced in ```json blocks or surrounded by prose by extracting the first
    top-level ``[...]`` span.
    """
    if not content or not content.strip():
        return [], "empty response"

    # If the entire content is a single JSON object/dict (or markdown-fenced one),
    # reject it as it is not a JSON array of objects.
    try:
        stripped = content.strip()
        if stripped.startswith("```"):
            lines = stripped.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            stripped = "\n".join(lines).strip()
        single_val = json.loads(stripped)
        if isinstance(single_val, dict):
            return [], "response JSON is not an array"
    except Exception:
        pass

    raw = _extract_json_array(content)
    if raw is not None:
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                ops: List[Dict[str, Any]] = []
                for item in data:
                    if isinstance(item, dict):
                        ops.append(item)
                    else:
                        return [], "array contains a non-object op"
                return ops, None
        except Exception:
            pass

    # Fallback: try to find and parse individual JSON objects in the response
    # (e.g. if the model outputted separate objects or markdown blocks rather than a single array)
    ops = []
    start = 0
    while True:
        start_idx = content.find("{", start)
        if start_idx == -1:
            break
        depth = 0
        in_str = False
        esc = False
        end_idx = -1
        for i in range(start_idx, len(content)):
            ch = content[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end_idx = i
                    break
        if end_idx != -1:
            obj_str = content[start_idx : end_idx + 1]
            try:
                obj = json.loads(obj_str)
                if isinstance(obj, dict):
                    ops.append(obj)
            except Exception:
                pass
            start = end_idx + 1
        else:
            start = start_idx + 1

    if ops:
        return ops, None

    return [], "no valid JSON array or objects found in response"


def _extract_json_array(content: str) -> Optional[str]:
    """Return the first balanced top-level JSON array substring, or None."""
    start = content.find("[")
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(content)):
        ch = content[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return content[start : i + 1]
    return None
