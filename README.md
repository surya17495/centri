# Centri

**A memory-native personal agent. One durable, global memory remembers
everything you and your tools have ever done, so every turn — and every parallel
task — starts warm instead of cold.**

The goal is a Jarvis-style personal agent: one that knows you, carries context
across days and sessions, and acts through real tools. The binding constraint
today is the context window of current LLMs, not the ambition. Centri's answer is
to treat memory, not the context window, as the system of record: an append-only
**event spine** is the single global source of truth, **memory is a derived index**
that can be thrown away and re-derived, and every per-turn context is **assembled
fresh** by a deterministic curation function that attaches a receipt to every
line. The context window is a cache, not storage.

Memory is **global**: a session or thread is a *view* over the one memory, not a
boundary. Work done in one session is visible from another (cross-session
continuity), and the standing picture of "what's going on" updates as work
progresses, not just at session start (mid-session continuity). Parallel
tasks/spawns are *producers* of events into the same global spine, so several
threads of work can run without fragmenting memory.

Centri began as, and still ships with, a memory-first **coding** agent (an
OpenCode fork); the architecture is general-purpose and the coding loop is its
first fully-wired set of "limbs."

## What's in this repo

Three parts share one durable memory:

| Part | What | Lives in |
|------|------|----------|
| **Centri core** | Python memory API: append-only event spine, typed memory graph with bi-temporal supersession, deterministic curation, optional LLM consolidation, REST/WS surface. | [`core/`](core) |
| **OpenCode fork** | The TypeScript/Bun OpenCode app shell, patched (each patch marked `// CENTRI`) so every turn recalls a brief from the core and runtime events are tapped back into the spine. All memory calls **fail open**. | [`packages/opencode/src/centri/`](packages/opencode/src/centri) |
| **Hermes plugin** | A deployable `memory.provider` translating Hermes memory calls into the core's HTTP API. | [`deploy/hermes-plugin/centri/`](deploy/hermes-plugin/centri) |

The **event spine is the source of truth**; the memory graph is a derived,
re-derivable index over it. Centri is a fork of the MIT-licensed
[OpenCode](https://github.com/anomalyco/opencode) project — upstream attribution
is preserved in [`LICENSE-OPENCODE`](LICENSE-OPENCODE),
[`upstream/opencode/`](upstream/opencode/), [`FORK-NOTES.md`](FORK-NOTES.md), and
[`docs/centri-app.md`](docs/centri-app.md).

## Key features

- **One global, lossless event spine** — every tool call, file edit, decision,
  and result is durably appended (with secret redaction on write) and never
  mutated or deleted. Nothing is summarized away at write time, so the original
  wording is always recoverable. There is exactly one spine for the whole agent:
  sessions and threads are *views* over it, and parallel tasks/spawns are
  additional *producers* into it, so concurrent work never fragments memory.
- **Typed memory graph with bi-temporal supersession** — decisions, facts, open
  loops, identity, user profile, and concepts. New truth invalidates old truth,
  but history is retained: stale facts never resurface in a brief, while the full
  timeline stays auditable.
- **Deterministic curation with receipts** — each per-turn brief is rendered by
  one pure `curate()` path; every line carries a `source_event_id` pointing back
  to the ledger event it came from. The same `(graph, cue, budget, policy)`
  yields a byte-identical brief. No LLM runs at read time.
- **Verbatim recall with receipts** — a first-class turn capability: from any
  session, recall the *exact* original wording a user said earlier (byte-equal,
  not paraphrased or distilled), backed by a `source_event_id` receipt that
  resolves to the originating spine event. Built on a SQLite FTS5 index over the
  spine; the page-in is itself auditable (a `recall.verbatim` event). On no match
  the result is an honest empty, never a fabricated answer. This is the
  differentiator distillation-only memory systems structurally cannot offer.
- **Receipt-backed global standing-self** — a small, slow-changing *ambient
  digest* (identity, active projects, top open loops, a short rolling
  current-work narrative, and a **user profile** of conventions you repeat) is
  derived from the live graph with no thread filter, so it reflects every
  session and producer. It carries `source_event_id` and a bounded `derived_from`
  receipt list, so the standing picture is auditable back to the events that
  produced it. Refreshed by the consolidation worker as work lands.
- **Temporal recall** — ask "what changed since yesterday" or "where did we leave
  off" and get an answer grounded in the ledger's timeline, not a guess.
- **Skills / procedural memory** — beyond *what* happened (episodic) and *what is
  true* (semantic), the core keeps a procedural store of *how to do things*:
  reusable procedures learned from successful runs are recorded as
  `procedural.memory` events and paged back in on relevant turns. This is the
  seam for first-class skills; richer authoring and surfacing is on the roadmap.
- **Tools / limbs over ACP** — work is handed to a coding agent (Agent Client
  Protocol, JSON-RPC over stdio) and to a first-class tool contract, with live
  streaming progress, an approval gate for destructive actions, and failover to a
  fallback hand. Coding (via the OpenCode fork) is the first fully-wired limb;
  a browser/automation limb (Playwright) is on the roadmap.
- **First-class tool contract** — tools (Composio, e.g. Tavily search) sit beside
  the coding hands. Every invocation is event-ledgered, side-effectful tools pass
  through the approval gate, read-only results fold back into memory.
- **History import** — a one-shot bootstrap plus a continuous tail of OpenCode,
  Claude Code, and Cursor histories, so a fresh install starts with complete
  memory instead of a blank slate.
- **Hermes structured ingestion** — Hermes chat is ingested as typed, dedupable
  envelopes (`hermes.user.message`, `hermes.assistant.message`,
  `hermes.tool.result`, `hermes.memory.write`), not flattened text.
- **Two-tier, role-based model support** — BYOK and model-agnostic via an
  OpenAI-compatible gateway. Cheap/fast roles (intent, fast reply, narration)
  and a heavier reasoning tier are configured independently per task
  (`MODEL_INTENT`, `MODEL_FAST_REPLY`, `MODEL_REASONING`, …), and a separate
  optional LLM consolidation tier runs offline behind the deterministic memory
  path. The orchestrator selects the role model; nothing is hardcoded.

## Architecture

Events are the source of truth; memory is a derived, re-derivable index over them.

| Layer        | What it is                                                                          |
|--------------|-------------------------------------------------------------------------------------|
| Web UI       | OpenCode fork web app — activity timeline, task cards, approvals. |
| Coordinator  | Python core loop: understand → decide → act → narrate → remember                    |
| Event spine  | Append-only SQLite ledger + in-memory bus, with secret redaction on write. One global truth; threads/sessions are views, spawns are producers. |
| Memory       | Typed graph — decisions, facts, open loops, identity/profile, concepts — with bi-temporal supersession, derived from the spine |
| Standing-self | Receipt-backed ambient digest derived globally from the graph; verbatim recall lifts exact prior wording on demand. Rehydrated before turns. |
| Hands / limbs | Capability router over coding agents — real ACP client, OpenCode fallback (browser/Playwright limb planned) |
| Tools        | `ToolProvider` contract with Composio; event-ledgered, approval-gated invocation    |
| Models       | Two-tier, BYOK, model-agnostic via an OpenAI-compatible gateway; role models set per task |

See [`docs/README.md`](docs/README.md) for the full index, or
[`docs/architecture.md`](docs/architecture.md),
[`docs/memory-architecture.md`](docs/memory-architecture.md), and
[`docs/event-contract.md`](docs/event-contract.md) for detail.

## Quickstart

### From source

```bash
cd core
python -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"

cp ../.env.example ../.env  # fill in your keys (BYOK)
python -m pytest tests/ -v  # run the suite
centri                      # start the server (or: python -m centri.cli)
```

The API listens on `127.0.0.1:8760` by default. Health checks:

```bash
curl localhost:8760/health
curl localhost:8760/status
```

### OpenCode fork

The OpenCode app shell (web + TUI) is the default client. Build and run it from
[`packages/opencode/`](packages/opencode):

```bash
cd packages/opencode
bun install
bun dev          # TUI
bun web          # web UI on :4096
```

Point it at the core via `CENTRI_URL` and `CENTRI_TOKEN` environment variables
(see [`.env.example`](.env.example)). The fork transparently taps runtime events
into the memory spine and recalls a brief before each turn.

## Configuration

All configuration is environment-driven — copy [`.env.example`](.env.example) to
`.env` and fill in your keys. Nothing is committed; `.env` and `*.db` are
gitignored. Centri is BYOK (bring your own keys) and model-agnostic:

- **Model gateway** — `LITELLM_BASE_URL` / `LITELLM_API_KEY` point at any
  OpenAI-compatible provider. Role models are set per task (`MODEL_INTENT`,
  `MODEL_REASONING`, …).
- **Auth** — `CENTRI_AUTH_TOKEN` gates every REST route except `/health` (and the
  `/events/stream` WebSocket via `?token=`). Empty means auth off; set it before
  exposing a port.
- **Tools** — `CENTRI_COMPOSIO_API_KEY` enables Composio; `CENTRI_COMPOSIO_TOOLS`
  is a comma-separated allowlist (default `TAVILY_SEARCH`). With no key the
  provider reports unavailable-with-reason and never touches the network.
- **Embeddings (optional)** — off by default (lexical + FTS5 recall only). Turn
  on semantic recall with `CENTRI_EMBEDDING_*`; see [`.env.example`](.env.example).
- **Ingest paths** — `CENTRI_INGEST_OPENCODE_PATHS`,
  `CENTRI_INGEST_CLAUDE_CODE_PATHS`, and `CENTRI_INGEST_CURSOR_PATHS` override the
  per-platform probe for histories in unusual locations;
  `CENTRI_INGEST_DISABLED_AGENTS` opts an agent out.

## Service startup

The reference deployment runs two services sharing one memory DB
(`~/.centri/state.db`):

| Service | Port | What |
|---------|------|------|
| `centri-core.service` | 8760 | The Centri core (`centri serve`). |
| `opencode.service` | 4096 | The OpenCode fork web UI (`opencode web … --port 4096`), pointed at the core via `CENTRI_URL` / `CENTRI_TOKEN`. |

```bash
sudo systemctl enable --now centri-core opencode
systemctl is-active centri-core opencode
curl -fsS http://127.0.0.1:8760/health    # -> {"status":"ok",...}
```

Full deployment guide (systemd, Caddy/TLS, ports): [`docs/DEPLOY.md`](docs/DEPLOY.md).
The `xdg-open` ENOENT line in the `opencode.service` logs is harmless — OpenCode
tries to auto-open a browser on a headless box and keeps running after the spawn
fails.

## Hermes integration

Centri also ships as a **Hermes `memory.provider`**. A thin plugin
(`CentriMemoryProvider`) translates Hermes memory calls into the core's HTTP API:
`prefetch` → `POST /memory/recall`, `sync_turn` / `on_memory_write` → batched
`POST /events/import`. The deployable plugin lives at
[`deploy/hermes-plugin/centri/`](deploy/hermes-plugin/centri/).

```yaml
# ~/.hermes/config.yaml
memory:
  provider: centri
  centri:
    api_base: http://127.0.0.1:8760
    auth_token: <CENTRI_AUTH_TOKEN>   # same value as the core + the fork's CENTRI_TOKEN
```

Restart Hermes after any plugin change. Full guide:
[`docs/HERMES-INTEGRATION.md`](docs/HERMES-INTEGRATION.md).

## Memory import

A fresh install imports your existing coding-agent histories so memory is
complete from day one. Discover what's available, then bootstrap once (idempotent
— re-running imports nothing new):

```bash
curl localhost:8760/ingest/discover            # "found N OpenCode messages, M Cursor sessions"
curl -X POST localhost:8760/ingest/bootstrap \
  -H 'content-type: application/json' -d '{}'   # one-time full import
```

After bootstrap, the ambient tail keeps pulling new history each scheduler tick.

## Benchmark

`centri-bench` is a falsifiable head-to-head: each engine assembles a per-turn
brief from the same seeded history, scored on brief completeness, re-proposal
rate, next-step correctness, and stale-fact handling. Native Centri is run
against a **real Letta server** (v0.16.8, pgvector archival) — both a
deterministic rubric and an LLM judge agree Centri wins, the gap entirely on
stale-fact supersession.

| Metric (avg over 3 personas) | Centri | Letta |
|------------------------------|:------:|:-----:|
| brief completeness ↑         | 1.00   | 1.00  |
| re-proposal rate ↓           | 0.00   | 0.00  |
| next-step correct ↑          | 1.00   | 1.00  |
| stale-fact correct ↑         | 1.00   | 0.67  |
| **composite ↑**              | **1.00** | **0.93** |

Letta is a **benchmark comparison only, not a runtime dependency** — Centri runs
without it. Run it yourself:

```bash
cd core
python -m centri.bench.run            # human-readable report
python -m centri.bench.run --json     # machine-readable scores
```

Methodology: [`docs/centri-bench.md`](docs/centri-bench.md); raw results:
[`docs/bench-results/`](docs/bench-results/).

## Status

**Implemented today** — the global event spine; the typed memory graph
(decisions, facts, open loops, identity/profile) with bi-temporal supersession;
deterministic curation with per-line receipts; verbatim recall with
`source_event_id` receipts; the receipt-backed global standing-self/ambient
digest with cross-session and mid-session continuity; temporal recall; the ACP
coding loop with approval gating and fallback; the Composio tool contract; and
history ingest (OpenCode, Claude Code, Cursor) plus Hermes structured ingestion.
These are covered by a green core suite (`cd core && python -m pytest`, 412
passing at the time of writing). The unit suite runs offline; integration tests
that need a live core, BYOK model keys, the `opencode` binary, or a real Letta
server are environment-gated and skip cleanly when those are absent. The OpenCode
fork web app builds and typechecks.

**Planned** — a browser/automation limb (Playwright); richer parallel-task /
spawn orchestration surfaced in the UI; a dedicated **memory view** in the web UI
(inspect the standing-self digest, the typed graph, and verbatim recall with their
receipts, so memory is legible and auditable from the client, not just the API);
first-class **skill authoring** over the procedural-memory seam; an optional LLM
digest summarizer behind the existing deterministic standing-self seam; and
broader tool coverage. The roadmap lives in [`docs/ROADMAP.md`](docs/ROADMAP.md);
the full docs index is [`docs/README.md`](docs/README.md).

**Build discipline** — increments are shipped small and end-to-end: build →
test → fix → push, never a skeleton. Every merged increment is expected to look
finished and work, with tests proving the behavior and receipts proving it is not
fabricated.

## License

Centri is licensed under the **MIT License** — see [LICENSE](LICENSE). The
OpenCode app shell in [`packages/opencode/`](packages/opencode/) is a fork of the
MIT-licensed [OpenCode](https://github.com/anomalyco/opencode) project; its
upstream license is preserved at [`LICENSE-OPENCODE`](LICENSE-OPENCODE) and the
original upstream README is kept at [`upstream/opencode/`](upstream/opencode/).
Runtime dependencies are permissively licensed (MIT/BSD/Apache-2.0);
`letta-client` is bench-only tooling, not a product dependency.
