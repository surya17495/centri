# CENTRI Roadmap

Design principle across every phase: **events are the source of truth; memory is
a derived, re-derivable index.**

## Decisions (ratified 2026-06-11)

Three vision decisions the owner ratified; they bound the scope of the phases
below.

1. **Cross-channel continuity = shared core, not a sync layer.** All clients
   (desktop Tauri, web, mobile PWA) talk to the *same* FastAPI + WebSocket
   server. There is **no** sync layer, offline cache, conflict resolution, or
   per-device cursor — explicitly **out of scope** unless a hosted/offline
   future forces it. The remaining multi-channel work is only: build/smoke the
   Tauri binary, deploy the React app as web, and add a PWA manifest for mobile.
   One memory, one server; separation lives only in the chat UI (see 3b.2).
2. **OpenCode-over-ACP is the canonical default coding hand.** Every hand is
   uniformly *an ACP agent identified by a launch command*; Cursor, Claude
   Code, etc. are **config entries, not code** (set `CENTRI_ACP_COMMAND`). The
   default is OpenCode-over-ACP (`acp_command="opencode acp"`, `acp` first in
   `hand_priority`). The native OpenCode subprocess hand is **demoted to a
   degraded fallback** (kept, not deleted). Real-binary verification is still
   pending on a real machine.
3. **Memory management stays deterministic — no LLMs in the consolidation
   loop.** The sleep cycle folds typed event hints into the graph with no model
   call. LLMs are allowed **only** behind optional seams that have deterministic
   fallbacks: (a) tiered summarization digests in 3c.1, (b) future semantic
   top-k recall. **Re-derivability from the event ledger remains an
   invariant** — the graph must always be reconstructable via
   `rebuild_from_events()`.
4. **North star — "OpenCode with photographic memory."** The wedge is OpenCode's
   simplicity and clean UI *plus* memory of everything we did; then voice input;
   then desktop-agent tools (browser, automations). The trajectory is Jarvis, but
   we get there by being the best OpenCode-with-memory first, not by reaching for
   Jarvis early. **Scope test for any feature:** *does this make us a better
   OpenCode-with-memory, or is it premature Jarvis?* If it is the latter, it
   waits.
5. **Single LLM config — users never configure LLM providers twice.** OpenCode's
   own provider config/auth is the **source of truth** for model access. The
   default coding hand (OpenCode-over-ACP) uses it natively, and CENTRI **reuses**
   it for the coordinator and the optional model seams wherever a provider key is
   resolvable there. Only *non-default* hands (Cursor, Claude Code, etc.) own
   their own config. `CENTRI_*` env keys still win when present; OpenCode's auth
   is a fallback; honest-unavailable when neither exists. The model catalog for UI
   display comes from **models.dev** (catalog only — LiteLLM remains the Python
   transport for actual calls). Key material is never written or logged; it is
   redacted in events.
6. **Context as cache.** State lives in the ledger/graph, **never** in
   conversation buffers. Per-turn context is assembled fresh by a pure, versioned
   curation function — the window is a *cache*, not storage. The bench metric for
   3c is **quality-per-token**: precision/recall of the facts a turn actually
   needed, per token spent. This reframes 3c's thesis: *make the context window
   obsolete as storage; curation, not accumulation.*
7. **Deterministic curation.** `brief = curate(graph_snapshot, cue, budget,
   policy_version)` is a **pure function** — no wall-clock, no randomness, no LLM
   at read time. Every brief line carries a score breakdown **and** a
   `source_event_id` receipt. The one exception is an optional **cue-expansion**
   seam: an LLM may rewrite an oblique ask into extra *query terms* — it may only
   EXPAND THE CUE, never select facts. Expansion terms are logged on the spine;
   the deterministic builder is the fallback when the seam is unconfigured.
8. **No visible remembering + ambient layer.** Memory must feel human: the user
   never sees retrieval mechanics ("searching memory…", "found 3 facts").
   Receipts are available on demand, invisible by default. Briefs have two
   layers: **(a) ambient** — small, slow-changing standing context present in
   *every* brief (identity/conventions, active projects, top open loops, a short
   narrative of the recent past), refreshed by consolidation, within its own
   ~few-hundred-token budget; **(b) cued** — per-turn ranked retrieval. The
   "feels human" *unprompted* moments — waking-up situating ("while you were
   away…") and spontaneous association (surfacing an unusually-high-scoring past
   item as an aside) — run on this same machinery and are queued into 3d as the
   proactivity track.

### Decisions (ratified 2026-06-12, Phase A)

Four structural decisions made when the master plan (`docs/VISION.md`) was
ratified. They make the cheap-now / expensive-later moves immediately.

9. **Tenancy key now.** Every spine/graph row carries a `tenant_id` (default
   `"local"`). Single-tenant code paths may ignore it, but the schema, the event
   payload envelope, and **all new queries include it from now on**. *Rationale:*
   it is free to add today and a painful global migration later; it is the
   prerequisite for hosted mode (Phase 6). The key is laid down now; *enforcement*
   on every query path is Phase 6, not this decision.
10. **Voice transport = WebSocket audio (v1).** Voice audio frames ride the
    existing event-socket family for v1. *Rationale:* self-hosted LAN/localhost is
    the dominant case, so there is no NAT traversal to solve and one fewer infra
    dependency than WebRTC/LiveKit — which is reconsidered only if/when hosted mode
    demands it. STT is **local-first with a pinned model** (whisper-class),
    recorded like other policy stamps. (Implementation is Phase 5.)
11. **Tool abstraction (contract).** Tools are a **first-class contract parallel
    to Hand**: every tool invocation is an event with receipts; side-effectful
    tools require an **approval-gate event before execution**; tool output is
    ingestible by consolidation. Playwright is the first Tool. *This decision is
    spec only* — implementation is Phase 4.
12. **Retrieval = TEMPR-shaped, deterministic.** Adopt the proven multi-retriever
    + Reciprocal Rank Fusion pattern (Hindsight/Graphiti-validated) implemented
    over the spine: four parallel deterministic retrievers (lexical, graph-hop,
    temporal, stored-vector semantic) fused by **pure arithmetic**. Any reranker
    must be **pinned, local, and policy-stamped**. **No LLM judgment at read
    time** (reaffirms Decision 7). (Implementation is Phase 1 / 3c.1.)

### Decision (ratified 2026-06-12, owner)

13. **Photographic storage, human recall.** The spine is **photographic**:
    append-only, nothing is ever deleted; supersession retains history; digests
    are *derived views*; everything is re-derivable from the ledger. Recall is
    **human**: gist-first via curated, budgeted briefs with **zoom-in-on-demand**
    — and the zoom never fails, because every gist line carries a
    `source_event_id` receipt to verbatim ground truth. **Forgetting is a
    READ-TIME PRESENTATION POLICY, never write-time deletion.** The tiered digests
    of 3c.1 are the *gist layer over a lossless spine*, not "forgetting for
    realism." *The vision sentence: remembers everything verbatim, recalls like a
    person, verifies like a machine.* This decision reframes 3c.1's digests
    (derived views, never lossy storage) and is the structural reason curation
    quality must be **identical in chat and coding** — see 3c.0.2.

## Phase 0 — Foundation (this phase)

Port the HAL core into the `centri` package and establish the interfaces the rest
of CENTRI builds on.

- Python core ported from `project-jarvis` (`hal` → `centri`); Electron/TS and
  Telegram/LiveKit/voice surfaces dropped or stubbed honest-unavailable.
- Append-only SQLite event spine with **redaction before persistence**.
- `MemoryStore` interface + `SqliteMemoryStore` skeleton (core blocks, archival
  facts, synthesis hook, `rebuild_from_events()`).
- `Hand` ABC + capability router; `OpenCodeHand` (subprocess) and `AcpHand`
  (stub) both satisfy it.
- FastAPI app boots; `/health` and `/status` respond; vertical-slice test passes.

## Phase 1 — Coding loop (text-first) + Tauri shell  ✅ implemented

Status: backend + React frontend **sandbox-verified**; Tauri desktop binary
**scaffolded, needs a local Rust toolchain** to compile (cargo absent from the
build sandbox).

- ✅ Real ACP wire protocol (JSON-RPC over stdio) in `AcpHand`: initialize →
  session lifecycle → prompt turns, streaming `session/update` mapped to live
  `task.progress`/`hand.progress`, `session/request_permission` round-tripped
  through the approval gate, and cancellation. Verified by `test_acp_hand.py`
  against a scripted fake ACP agent over real stdio.
- ✅ Router prefers a healthy ACP hand and falls back to the OpenCode subprocess;
  health is reported honestly for both. ACP command is configurable per hand.
- ✅ Streaming seam: hand progress flows hand → jobs → event bus → WebSocket live
  (no completion-only capture). Destructive permissions surface as
  `approval.requested`; UI resolution returns over ACP; timeout denies with reason.
- ✅ Delegation-brief seam (`build_delegation_brief`): active repo + recent related
  task summaries + core memory blocks assembled into the hand brief. (Full
  cue-driven memory injection is Phase 2.)
- ✅ Tauri 2 + React + TS + Tailwind desktop shell in `shell/` over HTTP +
  `/events/stream`: activity timeline, streaming task cards, inline approval cards,
  command bar, status strip, settings panel. Runs in-browser via `vite dev`;
  `tsc --noEmit` + `vite build` + vitest component tests pass.
- ✅ End-to-end text-first coding loop verified by `scripts/smoke_phase1.sh`
  (command → task → streamed events → approval round-trip over a live WebSocket).
- ⏳ Tauri desktop binary: scaffolded (`shell/src-tauri/`, single resizable window,
  480px min, dark theme, capabilities, global-shortcut stub) but must be built
  locally with cargo — not verified in-sandbox.

## Phase 2 — Memory v1 + briefing  ✅ implemented

Status: backend **sandbox-verified** (74 backend pytest tests green; `centri-bench`
runs native-vs-Letta with native at 1.00 composite, Letta at 0.93). Scoring uses a
deterministic rubric (no model key in the sandbox) with an LLM-judge seam; see the
honest accounting in the README.

Design: [`memory-architecture.md`](memory-architecture.md). Benchmark:
[`centri-bench.md`](centri-bench.md).

- ✅ Memory synthesis worker ("sleep cycle", `consolidation.py`): folds event hint
  batches into typed decision/fact/open-loop objects with receipts
  (`source_event_id`), never freeform prose; conflicts resolved by supersession; the
  scheduler drives it on a high-water mark each tick.
- ✅ Typed memory graph (`memory_graph.py`) on SQLite: bi-temporal supersession (new
  truth sets `superseded_by`/`invalidated_at`, history retained, live view current
  only); never confabulates (`OUTCOME_UNKNOWN`).
- ✅ Cue-driven injection (`memory_brief.py`): assembles relevant decisions,
  rejections, conventions, and open alternatives into the hand brief via
  `build_delegation_brief()` at delegation/session-start/repo-open.
- ✅ Proactive briefing ("what changed, what's blocked, what's next", `GET /briefing`)
  plus dormancy detection (one yes/no line per dormant loop, surfaced once).
- ✅ Re-derivability via `rebuild_from_events()` (the bench native backend rebuilds
  the whole graph from the ledger before assembling each brief).
- ✅ Escape-hatch validation: `LettaMemoryStore` adapter run head-to-head against
  CENTRI native in `centri-bench` (`python -m centri.bench.run`).

**Anti-gaming rule:** `centri-bench` tasks are written *before* Phase 2
implementation starts — `docs/centri-bench.md` is that commitment, so the
implementation cannot quietly target the test.

## Phase 3a — VM deployment hardening  ✅ implemented

Status: auth + deploy bundle **sandbox-verified**; systemd/Caddy/Let's Encrypt
need a real VM (see [`../deploy/README.md`](../deploy/README.md)).

- ✅ Shared-secret bearer auth (`CENTRI_AUTH_TOKEN`): every REST route except
  `/health`; WebSocket via `?token=`; constant-time compare; 401s carry CORS
  headers. 5 `TestAuth` tests.
- ✅ Shell: auth token field in Settings; token on every fetch + WS URL.
- ✅ `deploy/`: idempotent `install.sh` (venv, generated token, systemd unit,
  optional Caddy auto-TLS), `centri.service`, `Caddyfile`.

---

# The vision gap

**Vision: a stateful agent that remembers everything we did and pulls the right
context before being asked — one memory across every client, separation only in
the chat UI.**

What that decomposes into, and the phase that closes each piece:

| Gap | Phase |
| --- | --- |
| Hands record summaries, not full transcripts | 3b.1 |
| One global timeline; no chat separation (`thread_id` unused) | 3b.2 |
| Work done outside CENTRI (Cursor/OpenCode-direct) invisible | 3b.3, 3b.4 |
| A fresh install starts with empty memory (no prior history) | 3b.4 |
| Open loops tracked but never proactively closed | 3d.1 |
| Flat consolidation won't hold precision at 10^6 events | 3c |
| Memory quality unmeasured between releases | 3e |

Per Decision 1, the "one memory across every client" half of the vision is
**already structurally closed**: all clients hit the same FastAPI + WS core, so
continuity is a property of the shared server, not of a sync layer. The only
multi-channel work left is surface delivery — build/smoke the Tauri binary,
deploy the React app as web, add a PWA manifest for mobile — with no offline
cache / conflict resolution / device cursors unless a hosted/offline future
demands them.

## Phase 3b — Capture completeness

Make the spine actually see everything. Small, independent pieces — each lands
with tests and its own commit.

- **3b.1 Full hand transcripts.** ACP `agent_message_chunk` / tool-call text
  currently truncates to 240 chars in `task.progress` summaries. Add
  `hand.transcript` events carrying full text (chunk-coalesced per turn), keep
  the short summaries for the UI, and let consolidation read transcripts.
  Acceptance: a delegated session's full reasoning is recoverable from the
  spine verbatim; consolidation hints include transcript content; pytest green.
- **3b.2 Threads.** `events.thread_id` + `threads` table exist; nothing uses
  them. `/utterance` accepts `thread_id` (creates on first use); `/events` and
  `/threads` filter by it; shell gets a minimal thread sidebar (list, new,
  switch — timeline scoped to active thread). Memory stays global — that is the
  point. Acceptance: two threads with disjoint chat, one memory graph; brief in
  thread B cites facts from thread A. **Done (2026-06-11):** `/utterance`
  `thread_id` (create-on-first-use, `th-default` fallback), `/events?thread_id=`
  filter, POST `/threads`, `ThreadSidebar` + thread-scoped `useEventStream`;
  `/briefing` + `/memory/graph` stay unscoped. See `HANDOFF.md`.
- **3b.3 Ingestion adapters.** Tail external session stores (OpenCode
  `opencode.db` first — richest) into spine events
  (`ingest.opencode.message`, importance low, redaction applied). Idempotent
  incremental sync (high-water mark per source), then consolidation digests
  them like native events. Acceptance: point at a fixture opencode.db, events
  appear once (re-run = no dupes), facts from an ingested session surface in a
  brief. **Done (2026-06-11).** See `HANDOFF.md`.
- **3b.4 Memory bootstrap.** A fresh install should discover and import the
  user's *existing* coding-agent histories so memory is complete from day one —
  not just from the moment CENTRI was installed. **Inserted ahead of 3d.1**
  (ratified 2026-06-11): the value of proactivity (3d) depends on memory already
  being populated, so seeding it comes first. Rationale that makes this cheap:
  since ingestion is high-water-mark based, **a one-time import and the
  continuous tail are the same code path** — *bootstrap = first tick* (the only
  difference is the HWM starts empty). Generalize 3b.3's lone ingestor into an
  **adapter registry**: per-agent adapters (OpenCode, Claude Code, Cursor) that
  share the existing HWM / idempotency / redaction / write core, so a new agent
  is a reader plus labels, not a pipeline. Add **discovery** (probe well-known
  default paths per platform; honest-unavailable when nothing is found) exposed
  at `GET /ingest/discover` so a client can say "found 1,400 OpenCode messages,
  600 Claude Code sessions — import?" before committing; and **bootstrap** at
  `POST /ingest/bootstrap` that runs a full import across discovered (or
  configured) sources, emitting `ingest.bootstrap.*` progress events on the spine
  so the shell timeline shows it. New adapters are read-only + schema-tolerant
  like 3b.3 (degrade honestly — skip with a logged reason — when tables/files are
  missing). Claude Code = session JSONL under `~/.claude`; Cursor = local
  `state.vscdb` KV chat tables. Per-agent path overrides / disables via config.
  Acceptance: fixture stores for each agent import once (re-run = no dupes),
  per-source HWM, redaction applied, discovery returns counts, bootstrap is
  idempotent and emits progress. **Done (2026-06-11).** Real Claude Code/Cursor
  data verification remains on the real-machine list (fixture-verified only). See
  `HANDOFF.md`.

## Phase 3c — Context as cache (quality-per-token)

**Thesis (Decisions 6–8):** make the context window obsolete *as storage* — the
window is a cache; the metric is quality-per-token; curation must feel human.
State lives in the ledger/graph and is assembled fresh per turn by a pure,
versioned `curate()`. Precision must survive 10^6 events; the read path stays
history-independent.

- **3c.0 Deterministic context curation.** `curate(graph_snapshot, cue, budget,
  policy_version)`: a deterministic **cue builder** (alias expansion, thread
  anaphora, active-state signals, one graph hop), an **explicit-feature linear
  ranker** (hard filters first — superseded/redacted never enter; features =
  entity overlap, type prior, open-loop boost, thread affinity, recency as
  tiebreak only) with per-item score breakdowns, a **knapsack budgeter** (full
  text | one-line digest | dropped, by score; per-section floors) stamped with
  `policy_version` + graph high-water mark, and an **ambient standing-context
  layer** prepended to every brief. **Instrumentation:** `curation.miss` /
  `curation.waste` counters emitted with receipts as the feedback loop for
  replay tuning. The cue-expansion LLM seam is honest-unavailable (logged
  expansion terms, deterministic fallback). Wired into the live
  `build_delegation_brief()` path, not a side module. **Done (2026-06-12).**
- **3c.0.2 Universal per-turn curation.** Memory quality must be **identical in
  chat and coding** (Decision 13) — the chat/coding asymmetry is the biggest seam
  against the vision. Today the full `curate()` brief (ambient + cued layers,
  receipts, miss/waste) fires only on coding delegation
  (`build_delegation_brief`); plain chat turns get only
  `memory.recall(text, limit=3)`, often served stale from the hot cache. Fix:
  **every** utterance flows through the same `Curator` when wired — chat turns
  (`_handle_general`, and status/steering as appropriate) get the same curated
  ambient+cued brief, with receipts and `curation.brief`/miss-waste
  instrumentation, as coding delegation (so 3c.1 replay covers chat). `curate()`
  stays pure; the hot-cache fast path may still serve the AMBIENT layer instantly,
  but the cued layer comes from the live curator per turn (latency tradeoff
  documented honestly). `MemoryBriefAssembler` stays the bench fallback.
- **3c.1 Replay harness + quality-per-token bench + write-time embeddings.** A
  replay harness drives recorded spines + cues through `curate()` and scores
  quality-per-token against the `curation.miss`/`curation.waste` ledger; tiered
  daily→weekly digests + entity pages maintained by supersession (digest
  summarizer behind an optional LLM seam with a deterministic fallback, per
  Decision 3). Per Decision 13 the digests are **derived views over a lossless
  spine** — the gist layer, never write-time deletion; the spine stays
  photographic and every digest line keeps a receipt back to verbatim ground
  truth. **Write-time embeddings** stored on candidates so stored-vector
  similarity slots into the ranker as pure arithmetic (the Cue/candidate
  interfaces in 3c.0 are already shaped for it).
- **3c.2 Temporal queries.** "What changed since X", "where did we leave off"
  answered from the digest hierarchy.
- **3c.3 Aged-spine bench.** Synthetic 2-year/10^6-event spine; measure brief
  precision/latency vs corpus size. Regression-gate it.

## Phase 3d — Proactivity

- **3d.1 Waking-up + spontaneous association (the "feels human" track).** The
  unprompted half of Decision 8, running on 3c.0's curation machinery: a
  **waking-up** situating brief on the first interaction of a session/day ("while
  you were away…"), **spontaneous association** that surfaces an
  unusually-high-scoring past item as an aside, and the **open-loop scheduler** —
  scheduler tick scans live open loops with due/stale policies → emits
  `loop.nudge` events → surfaces in timeline + `/briefing` (and notification seam
  later). Acceptance: a loop created by a failed task nudges once after its
  policy window, never re-nudges unresolved; situating fires once per dormancy
  gap, not every turn.
- **3d.2 Watchers.** Long-running hand tasks report terminal state into the
  loop graph; completion closes the loop and produces a brief line.

## Phase 3e — Continuity bench (regression gate)

Extend `centri-bench` with the failure modes that motivated CENTRI (from the
user's real Hermes transcripts): unprompted cross-session awareness, fact
supersession under config churn, cold-start recall on a fresh client, awareness
of delegated-session work. Score native on every PR; alert on regression.

## Phase 4 — Voice

- Push-to-talk, streaming STT/TTS, barge-in; reintroduce `voice.*` event
  families behind the Phase 0 interface.

## Phase 5 — Productization

- Onboarding wizard, BYOK configuration UI, packaging/distribution.
- Bundled-subscription option for users who don't bring their own keys.
