# CENTRI

CENTRI is a voice-first, stateful, ambient builder agent for technical founders.
You talk to it about what you're building; it delegates the actual coding to
external coding agents and keeps durable, auditable track of everything that
happens. It is model-agnostic — bring your own LLM stack (BYOK) or, later, use a
bundled subscription.

Coding work is delegated through a **hand** abstraction. As of Phase 1 the default
hand is the Agent Client Protocol (ACP, JSON-RPC over stdio) client, which streams
live progress and round-trips destructive-action permission requests through
CENTRI's approval gate; any ACP-compatible agent (OpenCode, etc.) can be plugged in
by command. The original OpenCode CLI subprocess remains as a fallback hand.

The core design principle is that **events are the source of truth; memory is a
derived, re-derivable index.** Every runtime event is written to an append-only
SQLite ledger (after secret redaction), and CENTRI's memory — core context blocks
and archival facts — is a projection that can be thrown away and rebuilt by
replaying that ledger. Nothing in memory is authoritative; the spine is.

## Architecture

| Layer        | Status                        | What it is                                                        |
|--------------|-------------------------------|-------------------------------------------------------------------|
| Shell        | Phase 1: React verified, Tauri scaffolded | Tauri 2 + React desktop surface (text now, voice later) |
| Coordinator  | working                       | Python core: understand → decide → act → narrate → remember       |
| Event spine  | working                       | SQLite append-only ledger + in-memory bus, **redaction on write** |
| Memory       | Phase 2: typed graph + cue injection | Derived, re-derivable index over the spine; typed decisions/facts/open-loops with supersession; Letta adapter optional |
| Hands        | ACP + OpenCode working        | Capability router over the `Hand` ABC; ACP preferred, OpenCode fallback |

See [`docs/architecture.md`](docs/architecture.md),
[`docs/event-contract.md`](docs/event-contract.md), and
[`docs/ROADMAP.md`](docs/ROADMAP.md) for detail.

## Memory

Events are the source of truth; memory is a derived, re-derivable index. Memory is
modeled as four systems (episodic, semantic+supersession, prospective, procedural)
captured at the moment work happens and injected into hand briefs without the user
having to ask. Phase 0 ships the substrate (`MemoryStore`, `rebuild_from_events()`).
Phase 2 implements the typed memory graph (decisions/facts/open-loops with
bi-temporal supersession), the consolidation worker ("sleep cycle") that folds
event hints into that graph, cue-driven brief assembly, proactive briefing with
dormancy detection, and `centri-bench` — the falsifiable benchmark. The design and
benchmark are specified in
[`docs/memory-architecture.md`](docs/memory-architecture.md) and
[`docs/centri-bench.md`](docs/centri-bench.md).

Run the benchmark:

```bash
cd core
python -m centri.bench.run            # human-readable report
python -m centri.bench.run --json     # machine-readable scores
```

## Dev setup

```bash
cd core
python -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"

# configure (BYOK): copy the example and fill in your keys
cp ../.env.example ../.env

# run the tests
python -m pytest tests/ -v

# start the server
centri          # or: python -m centri.cli
```

The API listens on `127.0.0.1:8760` by default. Quick check:

```bash
curl localhost:8760/health
curl localhost:8760/status
```

## What works now (honest)

This distinguishes **sandbox-verified** (proven in CI/the dev sandbox) from
**needs-local-build** (correct by construction but requires a toolchain not present
in the sandbox — namely Rust/cargo for the Tauri desktop binary).

- **Sandbox-verified (backend):** event spine with redaction-before-persistence;
  FastAPI app with `/health`, `/status`, `/utterance`, tasks/approvals/threads/events,
  `/events/stream` WebSocket; coordinator intent → handoff → job loop; the **real ACP
  hand** speaking JSON-RPC over stdio (initialize → session lifecycle → prompt turns,
  streaming `session/update` → live `task.progress`/`hand.progress`, permission
  requests → approval gate, cancellation); router prefers a healthy ACP hand and falls
  back to the OpenCode subprocess; delegation-brief seam enriching hand briefs with
  recent task summaries; SQLite memory store with `rebuild_from_events()`; BYOK model
  router. Covered by `pytest core/tests/` (74 tests incl. ACP client tests against a
  scripted fake agent, plus the Phase 2 memory suite) and the end-to-end
  `scripts/smoke_phase1.sh` (command → task → streamed events → approval round-trip
  over a live WebSocket).
- **Sandbox-verified (Phase 2 memory):** typed memory graph (`memory_graph.py`) with
  decisions/facts/open-loops and bi-temporal supersession (new truth invalidates old,
  history retained, live view shows only current); consolidation worker
  (`consolidation.py`) folding typed event hints into the graph and re-deriving it
  from the ledger via `rebuild_from_events()`; cue-driven brief assembly
  (`memory_brief.py`) wired into `build_delegation_brief()`; proactive briefing
  (`GET /briefing`) and memory inspection (`GET /memory/graph`); scheduler dormancy
  detection (one yes/no line per dormant loop, surfaced once). Falsifiable via
  `centri-bench` (`python -m centri.bench.run`): native CENTRI scores **1.00**
  composite vs the Letta-style prose-archival adapter at **0.93**, with the gap on
  stale-fact supersession (native 1.00 vs Letta 0.67) — the central thesis. See the
  honest accounting below.
- **Sandbox-verified (shell frontend):** the React app in `shell/` builds and
  typechecks (`tsc --noEmit` + `vite build`) and runs in a plain browser via
  `npm run dev` — activity timeline, streaming task cards, inline approval cards,
  command bar, status strip, settings panel. Component tests pass under vitest.
- **Needs-local-build:** the Tauri 2 desktop wrapper (`shell/src-tauri/`). Fully
  scaffolded (single resizable window, 480px min, dark theme, capabilities,
  global-shortcut stub) but `cargo`/Rust is not available in the sandbox, so
  `npm run tauri build` must be run on a machine with the Rust toolchain. See
  [`shell/README.md`](shell/README.md).
- **Honest-unavailable:** voice endpoints (Phase 3); Letta semantic memory unless
  `CENTRI_LETTA_URL` is configured. These report unavailable-with-reason rather than
  faking success.
- **Not here yet:** voice (Phase 3), subscriptions, packaging (Phase 4).

### centri-bench: honest accounting

The benchmark reproduces the methodology of [`docs/centri-bench.md`](docs/centri-bench.md)
faithfully, with two documented deviations forced by the sandbox:

- **Deterministic rubric, not an LLM judge.** The spec calls for LLM-judge grading
  with human spot-checks; the build sandbox has no model API key. Scoring
  (`bench/scoring.py`) implements the rubric the judge would apply against
  *structured* ground truth (exact rejected approaches, required brief substrings,
  stale/current pairs, the next step) authored in `bench/personas.py` *before* the
  implementation — so it is the judge's checklist made executable, not a softer test.
  A `set_judge()` seam slots an LLM judge in unchanged when keys are present.
- **Incumbents out of scope; Letta in local-projection mode.** Hermes, Claude Code,
  and Cursor cannot ingest the typed event ledger, so they are out of scope for an
  in-process harness — per the spec this handicap *is* the point. The `LettaMemoryStore`
  adapter ran in local-projection mode (no Letta server in the sandbox); it models
  Letta's prose-archival storage, which has no typed supersession.
- **Known metric limitation:** the Letta adapter scores 1.00 on *brief completeness*
  because dumping all archival prose trivially includes every required substring; the
  completeness metric does not penalize the accompanying noise. The supersession
  failure is caught by the stale-fact metric (where Letta loses), but a precision/noise
  metric would widen the native lead further. Kept faithful to the spec's three
  headline metrics rather than adding one post-hoc.

## Configuration

All configuration is environment-driven (see `.env.example`). No secrets are
committed; `.env` and `*.db` are gitignored.
