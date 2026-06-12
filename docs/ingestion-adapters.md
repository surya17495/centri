# Ingestion adapters — the contract

An **ingestion adapter** makes coding work done *outside* CENTRI (a developer
running OpenCode / Claude Code / Cursor / some other agent directly) visible to
the memory graph. It is a **read-only tail** of one external session store: it
reads new messages since a per-source high-water mark, normalizes each into an
`ingest.<tool>.message` spine event, and lets consolidation digest them exactly
like native events.

This document is the contract for writing one. See `centri/ingest/base.py` for
the shared machinery and `centri/ingest/{opencode,claude_code,cursor}.py` for
worked examples. If your agent's store is a plain JSONL file or a SQLite chat
table, you may not need a new class at all — use the **generic config-driven
adapter** (below).

## What the base class gives you for free

`centri.ingest.base.MessageAdapter` owns everything that must be identical across
agents, so an adapter is *a reader plus a few labels*, not a pipeline:

- **High-water mark (HWM)** — incremental sync. Each pass reads only rows past the
  stored cursor (`"<ts>|<id>"`), persisted per `source` in the `ingest_state`
  table. Bootstrap is just the first tick with an empty HWM.
- **Idempotency** — deterministic event ids (`ingest:<source>:<row-id>`) plus an
  `event_exists` guard. Re-running over the same store produces no duplicates,
  even if the HWM is reset.
- **Redaction** — events are written via `db.append_event`, which scrubs secrets
  before persistence. You never redact yourself.
- **Fact hints** — assistant/tool/model turns carry a typed `fact` hint that
  consolidation folds into the graph (surfaces in briefs). User prompts are
  captured as events but **not** folded — a bare question is not a decision, and
  consolidation must not confabulate one.
- **Live publish** — each appended event is published on the event bus so the
  shell timeline updates live.

## What you must supply

Subclass `MessageAdapter` and set the labels:

```python
class MyAgentIngestor(MessageAdapter):
    agent = "myagent"                       # registry key + discovery label
    tool = "myagent"                        # appears in payload + fact statements
    event_type = "ingest.myagent.message"   # spine event type
    event_source = "ingest.myagent"         # spine event source
    source_prefix = "myagent"               # HWM source-key prefix
    fact_tags = ("ingest", "myagent", "transcript")
```

Then implement two methods:

1. **`read_messages(self, path: Path) -> list[dict]`** — open the external store
   **read-only** and return normalized message dicts. Each dict is:

   ```python
   {"id": str, "session_id": str, "role": str, "content": str, "ts": str}
   ```

   - `id` must be **stable** across runs (idempotency depends on it). If the store
     has no stable id, synthesize a deterministic one (e.g. `f"{file_stem}:{lineno}"`).
   - `role` drives fact-folding: `assistant` / `tool` / `model` fold; everything
     else is captured but not folded.
   - `ts` should be ISO-8601 where possible — use `coerce_ts()` from `base` to
     normalize epoch ms/seconds.
   - Use `flatten_content()` for content that may be a JSON "parts" array.
   - Use `first_present()` to pick the first present column from a candidate list.
   - **Read-only & schema-tolerant.** Open SQLite with `?mode=ro`. Tolerate
     column/key/JSON-shape variation across agent versions. Raise on a genuinely
     broken store (discovery surfaces it as unavailable); return `[]` for an
     empty-but-valid one.

2. **`default_locations(self) -> list[Path]`** — well-known default paths to probe
   during discovery, most-likely first, per platform (macOS/Linux today).

Optionally override `_cheap_count()` for a fast discovery count (e.g.
`SELECT count(*)` instead of a full read), and `fact_topic()` / `fact_statement()`
to tune how a session surfaces as a fact.

## Registering an adapter

Add the class to `_ADAPTER_CLASSES` in `centri/ingest/registry.py` (the dict order
is the discovery / bootstrap order). It then participates in `GET /ingest/discover`
and `POST /ingest/bootstrap` automatically, honoring `IngestConfig` path overrides
and disables.

## The generic config-driven fallback (no new class)

For an unknown agent whose store is a plain **JSONL** file or a **SQLite chat
table**, use `centri.ingest.generic.GenericIngestor` with a
`GenericAdapterConfig` — no subclass required:

```python
from centri.ingest import GenericIngestor, GenericAdapterConfig

# JSONL with custom field names
cfg = GenericAdapterConfig(
    agent="acme",
    kind="jsonl",
    role_field="speaker",
    content_field="utterance",
    ts_field="created",
    locations=["~/.acme/sessions"],
)
adapter = GenericIngestor(db, cfg, event_bus=bus)
await adapter.ingest("~/.acme/sessions/today.jsonl")

# SQLite chat table with custom columns
cfg = GenericAdapterConfig(
    agent="acme",
    kind="sqlite",
    table="chat_messages",
    role_field="sender",
    content_field="body",
    ts_field="created_at",
)
```

Configured field/column names are tried first, then the same generous candidate
lists the built-in adapters use, so a slightly-off config still finds data. The
generic adapter reuses the identical HWM / idempotency / redaction / fact-hint /
write core — it is the contract above, parameterized instead of subclassed.

## Honesty

New adapters built against varying on-disk schemas should be marked
**fixture-verified only** in `HANDOFF.md` until proven against real on-disk data
on a real machine — schemas drift across releases, and a tolerant reader is not
the same as a verified one.
