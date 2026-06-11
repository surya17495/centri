# CENTRI

CENTRI is a voice-first, stateful, ambient builder agent for technical founders.
You talk to it about what you're building; it delegates the actual coding to
external coding agents and keeps durable, auditable track of everything that
happens. It is model-agnostic — bring your own LLM stack (BYOK) or, later, use a
bundled subscription.

Coding work is delegated through a **hand** abstraction. Every hand is uniformly
*an ACP agent (Agent Client Protocol, JSON-RPC over stdio) identified by a launch
command* — the client streams live progress and round-trips destructive-action
permission requests through CENTRI's approval gate. The **canonical default hand
is OpenCode-over-ACP** (`CENTRI_ACP_COMMAND=opencode acp`); other agents (Cursor,
Claude Code, …) are config entries, not new code — just point the command
elsewhere. The native OpenCode CLI subprocess hand is retained as a **degraded
fallback** for when no ACP peer is reachable.

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
  composite vs a **real Letta server** (pgvector archival, `letta_http` mode) at
  **0.93**, with the gap on stale-fact supersession (native 1.00 vs Letta 0.67) — the
  central thesis. See the honest accounting below.
- **Sandbox-verified (shell frontend):** the React app in `shell/` builds and
  typechecks (`tsc --noEmit` + `vite build`) and runs in a plain browser via
  `npm run dev` — activity timeline, streaming task cards, inline approval cards,
  command bar, status strip, settings panel. Component tests pass under vitest.
- **Sandbox-verified (Phase 3a deployment hardening):** shared-secret bearer auth
  (`CENTRI_AUTH_TOKEN`) on every REST route except `/health`, token-gated
  `/events/stream` WebSocket (`?token=`, since browsers cannot set WS headers),
  constant-time comparison, 401s that still carry CORS headers — covered by
  `TestAuth` (5 tests) and verified live (curl 401/200, WS handshake 403→101, full
  shell journey against a secured core with the token entered in Settings). The
  glassmorphism shell ships an Auth token field under Settings → Backend.
- **Needs a real VM (Phase 3a, honest):** [`deploy/`](deploy/README.md) provides an
  idempotent `install.sh` (venv + env file with generated token + systemd unit +
  Caddy auto-TLS reverse proxy), `centri.service`, and a `Caddyfile`. The script is
  syntax-checked and the auth flow it configures is sandbox-verified, but systemd
  lifecycle, Caddy install, and Let's Encrypt issuance need a real Ubuntu VM with a
  domain.
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

- **LLM judge + deterministic rubric (both run).** The spec calls for LLM-judge
  grading. `bench/judge.py` (`LLMJudge`, wired via the `set_judge()` seam) hands each
  assembled brief and the persona ground truth to a chat model and asks for strict
  JSON verdicts on the same metrics; it retries on malformed output and is env-driven
  (`CENTRI_JUDGE_BASE_URL`, `CENTRI_JUDGE_MODEL`). Run it with
  `python -m centri.bench.run --judge`. The deterministic rubric in `bench/scoring.py`
  remains the default and the offline cross-check: it grades the same *structured*
  ground truth (exact rejected approaches, required brief substrings, stale/current
  pairs, the next step) authored in `bench/personas.py` *before* the implementation.
  Run head-to-head against a **real Letta server** (`letta_http` mode, see the next
  bullet), **both graders agree**: native composite **1.00** vs Letta **0.93**, with
  the gap on stale-fact supersession (native 1.00 vs Letta 0.67). The judge
  reproducing the rubric independently is the point — the deterministic rubric is not
  a softer test, it is the judge's checklist made executable.

  | Metric (avg over 3 personas) | native (det.) | native (judge) | Letta (det.) | Letta (judge) |
  |------------------------------|:-------------:|:--------------:|:------------:|:-------------:|
  | brief completeness ↑         | 1.00          | 1.00           | 1.00         | 1.00          |
  | re-proposal rate ↓           | 0.00          | 0.00           | 0.00         | 0.00          |
  | next-step correct ↑          | 1.00          | 1.00           | 1.00         | 1.00          |
  | stale-fact correct ↑         | 1.00          | 1.00           | 0.67         | 0.67          |
  | **composite ↑**              | **1.00**      | **1.00**       | **0.93**     | **0.93**      |

  Judge model: `moonshotai/Kimi-K2.6` (temperature 0, strict JSON) via the sandbox
  relay. Judge wiring is unit-tested with a mocked HTTP layer (`tests/test_judge.py`,
  no network).
- **Incumbents out of scope; Letta scored against a real Letta server.** Hermes,
  Claude Code, and Cursor cannot ingest the typed event ledger, so they are out of
  scope for an in-process harness — per the spec this handicap *is* the point. For
  Letta we went past a local model and ran the head-to-head against a **real Letta
  server** (v0.16.8), stood up in the sandbox with no Docker: embedded Postgres +
  pgvector via the bundled `pgserver` binary, ORM-generated schema, the server
  configured to use the relay for both its LLM and embeddings. `LettaMemoryStore`
  routes to it over the `letta-client` SDK in `letta_http` mode
  (`core/src/centri/letta_http.py`): archival facts become real pgvector-backed
  *passages* retrieved by similarity, with no typed supersession. **The table above is
  the `letta_http` result** — native composite **1.00** vs the real Letta server's
  **0.93**, the gap entirely on stale-fact supersession (1.00 vs 0.67): on the webapp
  persona's `authsvc -> identity-gateway` rename, semantic passage retrieval returns
  *both* the stale and current note, exactly the accumulation failure CENTRI's typed
  graph avoids. Both graders (deterministic + LLM judge) agree on the genuine engine.
  `core/src/centri/bench/backends.py` labels which mode ran
  (`letta-adapter[letta_http]` vs `letta-adapter[local_projection]`) so the comparison
  is never silently faked; with no server configured the adapter degrades to a
  `local_projection` (lexical recall, same no-supersession contract) and says so.
  Re-run the live head-to-head with `scripts/live_letta_bench.sh` (set
  `CENTRI_LETTA_URL`). HTTP-mode wiring is also unit-tested with a mocked client
  (`tests/test_letta_http_store.py`, no network/SDK).
- **Known metric limitation:** the Letta adapter scores 1.00 on *brief completeness*
  because dumping all archival prose trivially includes every required substring; the
  completeness metric does not penalize the accompanying noise. The supersession
  failure is caught by the stale-fact metric (where Letta loses), but a precision/noise
  metric would widen the native lead further. Kept faithful to the spec's three
  headline metrics rather than adding one post-hoc.

## Configuration

All configuration is environment-driven (see `.env.example`). No secrets are
committed; `.env` and `*.db` are gitignored.
