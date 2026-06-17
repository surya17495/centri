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
        eid = ev.get("id") or ev.get("event_id") or "?"
        etype = ev.get("type") or "event"
        text = _event_text(ev.get("payload") or {})
        lines.append(f"{eid} [{etype}] {_truncate(text, per_event_chars)}")
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
    "Principles:\n"
    "- Be SELECTIVE but aim for HIGH RECALL: capture durable facts, decisions, and "
    "intentions; ignore transient chatter, greetings, and noise.\n"
    "- Prefer SMALL PRECISE ops over rewrites. To update an existing node, emit a "
    "single `supersede` targeting its node_id — do not restate the whole world.\n"
    "- OBSOLESCENCE DETECTION: examine the CURRENT MEMORY below alongside the new "
    "activity. If any existing decision, fact, or open loop is now obsolete — "
    "superseded by newer decisions, contradicted by newer facts, or the project/"
    "system it references has been abandoned or replaced — emit a `supersede` (for "
    "decisions/facts) or `close_loop` (for open loops) targeting its node_id. "
    "Examples: if 'adopt HAL as memory provider' is live but the new activity shows "
    "Centri was built to replace it, supersede the HAL decision. If an open loop "
    "about HAL is live but HAL is retired, close that loop.\n"
    "- ABSOLUTE DATES ONLY. Never write 'today', 'yesterday', 'recently', 'now'. "
    "If a time matters, use an explicit ISO date (YYYY-MM-DD). A statement with a "
    "relative time word will be REJECTED.\n"
    "- Dedupe against the CURRENT MEMORY shown below: if a fact already exists, do "
    "not re-add it; supersede it only if the new information actually changes it.\n"
    "- Propose profile updates (op: 'profile_update') when you see user preferences, "
    "work habits, project context, or configurations (e.g. key: 'active_projects', "
    "value: 'futures-agent, dashboard-next').\n"
    "- Every statement must stand on its own without the raw log — self-contained, "
    "specific, and verifiable.\n\n"
    "Allowed operations (emit a JSON array of these objects, nothing else):\n"
    '  {"op":"add_fact","topic":str,"statement":str,"tags"?:[str]}\n'
    '  {"op":"add_decision","topic":str,"statement":str,"stance"?:"adopted"|"rejected","rationale"?:str}\n'
    '  {"op":"open_loop","intent":str,"cue"?:str}\n'
    '  {"op":"close_loop","loop_id"?:str,"intent_match"?:str,"resolution"?:"done"|"parked"}\n'
    '  {"op":"supersede","node_id":str,"kind":"fact"|"decision","new_statement":str}\n'
    '  {"op":"profile_update","key":str,"value":str}\n'
    '  {"op":"finish"}\n\n'
    "Emit your ops as one JSON array, then a final {\"op\":\"finish\"}. If nothing "
    "is worth remembering, return [{\"op\":\"finish\"}]."
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
