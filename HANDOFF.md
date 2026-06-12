# HANDOFF — read this first if you are a fresh agent

Owner: surya (surya.munna95@gmail.com). Repo: https://github.com/surya17495/centri

## Non-negotiable working rules

1. **Commit and `git push origin main` after every completed piece** (~30–60 min
   of work). Pushed code is the only safe code; the session may die any time.
   Git identity: `-c user.name="surya17495" -c user.email="surya.munna95@gmail.com"`.
2. **Quality gates before every push:** `cd core && python -m pytest tests/ -q`
   (currently 208 passed) and, if `shell/` was touched,
   `cd shell && npm run typecheck && npm run test && npm run build` (14 tests).
3. **Be honest** in README/docs/reports about sandbox-verified vs
   needs-local-build (Tauri binary, real opencode binary, systemd/Caddy on a VM).
   Never claim something is tested when it isn't. The owner checks.
4. **Update this file** (Work queue + State) in the same commit as each piece.

## The vision (owner's words)

"A stateful agent that can remember everything we did in the past and pull the
right context before I have to ask." One memory across all clients
(desktop/web/mobile); separation lives only in the chat UI. See
`docs/ROADMAP.md` → "The vision gap" for the full decomposition.

## Decisions (ratified 2026-06-11) — do not relitigate without the owner

Canonical copy is `docs/ROADMAP.md` → "Decisions". Short form:

1. **Continuity = shared core, no sync layer.** All clients hit the same
   FastAPI + WS server. NO sync layer / offline cache / conflict resolution /
   device cursors — out of scope unless a hosted/offline future demands it.
   Remaining multi-channel work is only: build/smoke Tauri binary, deploy React
   as web, PWA for mobile.
2. **OpenCode-over-ACP is the default coding hand.** Every hand is "an ACP agent
   identified by a launch command"; Cursor/Claude Code/etc. are config entries,
   not code. Default is explicit now: `acp_command="opencode acp"`, `acp` first
   in `hand_priority`. Native OpenCode subprocess hand = degraded fallback (kept,
   not deleted). Real-binary verification still pending on a real machine.
3. **Deterministic memory — no LLMs in the consolidation loop.** LLMs allowed
   only behind optional seams with deterministic fallbacks: (a) tiered
   summarization digests in 3c.1, (b) future semantic top-k recall.
   Re-derivability from the event ledger (`rebuild_from_events()`) stays an
   invariant.
4. **North star — "OpenCode with photographic memory."** Wedge = OpenCode's
   simplicity + clean UI + memory of everything; then voice; then desktop-agent
   tools (browser/automations); trajectory = Jarvis. Scope test for every
   feature: *better OpenCode-with-memory, or premature Jarvis?*
5. **Single LLM config — never configure providers twice.** OpenCode's provider
   config/auth is the source of truth: the default hand uses it natively, CENTRI
   reuses it for the coordinator / optional seams where resolvable. Only
   non-default hands (Cursor, Claude Code) own their own config. `CENTRI_*` env
   keys win; OpenCode auth is fallback; honest-unavailable otherwise. models.dev
   is the UI model catalog (catalog only; LiteLLM is the Python transport). Key
   material never written/logged; redacted in events.
6. **Context as cache.** State lives in the ledger/graph, never in conversation
   buffers; per-turn context is assembled fresh by a pure, versioned curation
   function. Window = cache, not storage. 3c bench metric = quality-per-token.
7. **Deterministic curation.** `brief = curate(graph_snapshot, cue, budget,
   policy_version)` — pure: no wall-clock, no randomness, no LLM at read time.
   Every brief line has a score breakdown + `source_event_id` receipt. Optional
   cue-expansion seam may EXPAND THE CUE only (never select facts); expansion
   terms logged on the spine; deterministic fallback when unconfigured.
8. **No visible remembering + ambient layer.** User never sees retrieval
   mechanics; receipts on demand, invisible by default. Two brief layers:
   (a) AMBIENT — small slow-changing standing context in every brief
   (identity/conventions, active projects, top open loops, short recent-past
   narrative), refreshed by consolidation, own small budget; (b) CUED — per-turn
   ranked retrieval. Waking-up + spontaneous association = same machinery,
   unprompted, queued into 3d.

Phase A (ratified 2026-06-12) — see `docs/VISION.md` + `docs/ROADMAP.md`
"Decisions (Phase A)":

9. **Tenancy key now.** Every spine/graph row carries `tenant_id` (default
   `"local"`). Schema + event envelope + all NEW queries include it from now on;
   single-tenant paths may ignore it. Free now, painful migration later;
   prerequisite for hosted mode. Key now; enforcement is Phase 6.
10. **Voice transport = WS audio (v1).** Audio frames over the existing event
    socket family (self-hosted LAN/localhost dominant; no NAT, one fewer infra
    dep). WebRTC/LiveKit only if hosted demands it. STT local-first, pinned model,
    policy-stamped. Impl = Phase 5.
11. **Tool abstraction (contract, spec only).** Tools = first-class contract
    parallel to Hand: every invocation is an event w/ receipts; side-effectful
    tools need an approval-gate event before execution; output ingestible by
    consolidation. Playwright first. Impl = Phase 4.
12. **Retrieval = TEMPR-shaped, deterministic.** Multi-retriever (lexical /
    graph-hop / temporal / stored-vector) + RRF, pure arithmetic; any reranker
    pinned/local/policy-stamped; NO LLM at read time (reaffirms #7). Impl =
    Phase 1 / 3c.1.

Decision (ratified 2026-06-12, owner):

13. **Photographic storage, human recall.** Spine is photographic (append-only,
    nothing deleted, supersession retains history, digests are derived views,
    everything re-derivable). Recall is human (gist-first curated/budgeted briefs
    with zoom-in-on-demand that never fails — every gist line carries a
    `source_event_id` receipt to verbatim ground truth). **Forgetting is a
    READ-TIME PRESENTATION POLICY, never write-time deletion.** 3c.1 tiered digests
    = the gist layer over a lossless spine, not "forgetting for realism." Sentence:
    *remembers everything verbatim, recalls like a person, verifies like a
    machine.* Structural reason curation quality must be identical in chat and
    coding (→ 3c.0.2).

**Phase A is DONE (2026-06-12):** master plan (`docs/VISION.md`), the four
decisions above (`docs/ROADMAP.md` → "Decisions (Phase A)"), the tenancy-key
migration (Unit 3, code + 6 tests), and the owner verification checklist
(`VERIFY.md`). The phase numbering is now mapped onto VISION.md's Phases 0–6;
the work queue below is the same items under that mapping. **Next = 3c.1 /
Phase 1 (memory completion).**

## Phase 0 — real-machine verification (owner-run gate)

`VERIFY.md` (repo root) is the owner's ~1-hour real-machine pass. It turns every
"fixture-verified only" claim into "verified on real machine." Fill this table as
each step is run; any `no` keeps that demo claim hedged until it flips.

| Step | What it proves | Verified on real machine | Notes |
| --- | --- | --- | --- |
| 1 install + boot core+shell | documented quick-start starts on a real OS | _pending_ | |
| 2 discovery + bootstrap | real OpenCode/Claude Code/Cursor data imports w/ receipts | _pending_ | clears "adapters fixture-verified only" |
| 3 real ACP coding task | `opencode acp` binary drives a task; transcript+fact fold | _pending_ | clears "real-binary ACP pending" (Decision 2) |
| 4 provider reuse | OpenCode providers reused, no re-entered keys | _pending_ | clears "opencode config fixture-verified only" |
| 5 curation recall | real-history recall w/ receipts, no visible remembering | _pending_ | confirms Decisions 7 & 8 on real data |
| 6 Tauri build + models.dev | desktop bundle builds; live catalog fetch | _pending_ | clears "Tauri binary / models.dev live" |

## Work queue (do in order; each is one commit+push)

- [x] **3b.1 Full hand transcripts** — DONE. Both hands now record a
      `hand.transcript` event (full untruncated text; ACP also traces tool
      calls, OpenCode keeps stderr) plus a deterministic `fact` hint
      (`topic: delegated-session:<uid>`, tags `[hand, transcript, acp|opencode]`)
      that consolidation folds into the graph with a receipt. UI summaries stay
      240-char. Tests: `test_acp_hand.py::test_transcript_event_keeps_full_text`
      (fake agent `ACP_FAKE_LONG=1`), `test_consolidation.py::TestTranscriptHints`,
      `test_centri.py::TestHands` opencode transcript tests. pytest 108/108.
- [x] **3b.2 Threads** — DONE. `thread_id` wired end to end. POST `/utterance`
      accepts an optional `thread_id` (created on first use; absent → catch-all
      `th-default`); coordinator tags `user.utterance` + `coordinator.response`
      with the chat thread (`Coordinator._resolve_thread`/`_default_thread`).
      `/events?thread_id=` filters; new POST `/threads` creates an empty chat
      thread. Shell: `ThreadSidebar` (list/new/switch), `useEventStream(threadId)`
      resets + re-hydrates scoped on switch and filters live frames via
      `inThread` (frames with no thread stay global). Memory stays global —
      `/briefing` and `/memory/graph` are unscoped. Tests:
      `test_centri.py::TestThreads` (default-thread tag, explicit create-on-first-use,
      disjoint A/B chat, POST /threads); `ThreadSidebar.test.tsx`.
      pytest 112/112, vitest 9/9, tsc/build clean.
- [x] **3b.3 OpenCode ingestion adapter** — DONE (2026-06-11). Incremental,
      idempotent tail of an external `opencode.db` into `ingest.opencode.message`
      events. `centri.ingest.OpenCodeIngestor` opens the external DB read-only,
      tolerates column-name variants (id/session/role/content/ts, JSON "parts"
      flattened), normalizes each message to an event (`importance="low"`,
      `source="ingest.opencode"`), and writes via `db.append_event` so the
      redaction seam scrubs secrets. Idempotency = deterministic event id
      (`ingest:<source>:<external_id>` + `event_exists` guard) plus a persisted
      per-source high-water mark (`ingest_state` table, cursor `"ts|id"`).
      Assistant/tool messages carry a `fact` hint (`topic:
      opencode-session:<sid>`, tags `[ingest, opencode, transcript]`) that
      consolidation folds → surfaces in briefs; user prompts are captured but
      not folded (no confabulation). Scheduler `run_ingestion()` tails
      `CENTRI_OPENCODE_INGEST_DB` before consolidation each tick; POST
      `/ingest/opencode` does a one-shot ingest. Tests:
      `test_ingest_opencode.py` (11: idempotent re-run, incremental, per-source
      HWM, redaction, brief-surfacing, schema tolerance, missing-db, helper) +
      `test_centri.py::TestIngest` (2: endpoint idempotent, requires path).
      pytest 125/125.
- [x] **3b.4 Memory bootstrap** — DONE (2026-06-11). **Inserted ahead of 3d.1**
      by the owner: a fresh install should discover and import the user's existing
      coding-agent histories so memory is complete from day one. Key insight —
      since ingestion is HWM-based, one-time import and continuous tail are the
      *same code path*: **bootstrap = first tick** (HWM just starts empty).
      Generalized 3b.3's lone ingestor into an **adapter registry**
      (`centri.ingest.base.MessageAdapter` shared HWM/idempotency/redaction/write
      core + `centri.ingest.registry.IngestRegistry`). `OpenCodeIngestor` is now
      the first adapter in the registry; its 13 tests and `/ingest/opencode`
      config/endpoint contract are unchanged (`runtime.opencode_ingestor` still
      points at the OpenCode adapter). Two new adapters, read-only +
      schema-tolerant like 3b.3: **Claude Code** (`ClaudeCodeIngestor` — tails
      session JSONL under `~/.claude/projects`, `ingest.claude_code.message`) and
      **Cursor** (`CursorIngestor` — harvests chat from `state.vscdb` KV tables
      `ItemTable`/`cursorDiskKV`, `ingest.cursor.message`; degrades honestly when
      no KV/chat table). Assistant/tool turns carry fact hints (folded by
      consolidation); user prompts captured but not folded. **Discovery**
      (`GET /ingest/discover`) probes well-known default paths per platform
      (macOS/Linux) with cheap counts, honest-unavailable when absent.
      **Bootstrap** (`POST /ingest/bootstrap`) discovers + runs a full import
      across all sources, emitting `ingest.bootstrap.{started,progress,completed}`
      events on the spine (importance `normal`) so the shell timeline shows it; an
      explicit `sources` list is also accepted. Config overrides:
      `CENTRI_INGEST_{OPENCODE,CLAUDE_CODE,CURSOR}_PATHS` (extra probe paths) and
      `CENTRI_INGEST_DISABLED_AGENTS` (opt-out). Tests:
      `test_ingest_claude_code.py` (8), `test_ingest_cursor.py` (9),
      `test_ingest_registry.py` (6: discovery counts, disabled-agent, honest
      unavailable, bootstrap import + progress events, idempotent, explicit
      sources), `test_centri.py::TestBootstrap` (2: discover shape, bootstrap
      endpoint idempotent). **Honesty:** all new adapters are *fixture-verified
      only* — real Claude Code / Cursor on-disk data verification stays on the
      real-machine list (their schemas vary across releases; the readers are built
      tolerant but only proven against fixtures). pytest 150/150.
- [x] **3b.5 Onboarding + single LLM config** — DONE (2026-06-12). Three units.
      (1) **Decisions** — ratified north star ("OpenCode with photographic
      memory") + single-LLM-config in `docs/ROADMAP.md` / above. (2) **Backend
      single-config seam:** `centri.opencode_config.OpenCodeConfig` reads
      OpenCode's local config/auth read-only + schema-tolerant
      (`~/.config/opencode`, `~/.local/share/opencode`, `auth.json`/`opencode.json`),
      surfaces *configured providers* (`has_key` only — key material never
      returned to callers/events) in `GET /ingest/discover` + dedicated
      `GET /providers/discovered`; `ModelRouter` resolves a provider key from
      OpenCode auth as fallback when `CENTRI_*` env keys are absent (env wins,
      honest `None` when neither). `centri.models_catalog.ModelsCatalog` fetches
      models.dev with on-disk cache + TTL, honest-unavailable offline
      (`GET /models/catalog`); catalog-only, LiteLLM stays the transport — no
      hard dependency. `centri.ingest.GenericIngestor` is a config-driven
      fallback adapter (JSONL + SQLite chat tables, configurable fields) reusing
      the registry HWM/idempotency/redaction; adapter contract documented in
      `docs/ingestion-adapters.md`. (3) **Shell first-run onboarding:**
      `/ingest/discover` now carries a backend-derived `bootstrapped` flag
      (`db.has_ingest_state()` — NOT client localStorage); `OnboardingCard`
      shows "Found N OpenCode messages, M Claude Code…" with one-click import
      (`POST /ingest/bootstrap`), live progress from `ingest.bootstrap.*` spine
      events (no second socket), skip/dismiss, and a Settings "Memory import"
      re-run path. **Honesty:** OpenCode auth/config formats are *fixture-verified
      only* (schemas vary across releases; readers built tolerant). Tests:
      `test_opencode_config.py` (8), `test_models_catalog.py` (5),
      `test_ingest_generic.py` (6), `test_centri.py::TestSingleLlmConfig` (3),
      `OnboardingCard.test.tsx` (5). pytest 172/172, vitest 14/14, tsc/build clean.
- [x] **3c.0 Deterministic context curation** — DONE (2026-06-12). "Context as
      cache": per-turn context assembled fresh by a pure, versioned
      `brief = curate(graph_snapshot, cue, budget, policy_version)` — no
      wall-clock, no randomness, no LLM at read time, so the same inputs render a
      byte-identical brief. New `centri.curation`:
      **CueBuilder** (A — alias-table expansion via graph facts tagged `alias`,
      thread anaphora resolution from recent turns, active-state file/repo
      signals, one deterministic graph hop), **Ranker** (B — explicit-feature
      linear sum: overlap BM25-ish / type-prior decision>convention>fact>obs /
      open-loop-boost / thread-affinity, recency as TIEBREAK ONLY via a numeric
      ISO ordinal; hard filters via the graph's live views; per-item score
      breakdowns kept on every line), **Budgeter** (C — greedy knapsack by score,
      per-section floors so decisions/rejections never starve, full|one-line
      digest|drop; costs measured in REAL tokens via a pinned tiktoken
      `o200k_base` `TokenCounter`, honest `wordcount:v1` fallback recorded in the
      stamp when tiktoken is unavailable — never silent), **Ambient layer** (D — consolidation refreshes a standing
      digest stored as a reserved Fact `ambient-standing-context`, prepended to
      every brief in its own budget), and **miss/waste instrumentation** (E —
      `compute_miss_waste`, emitted as `curation.brief` with receipts;
      `curation.cue` logs cue-expansion provenance). The optional LLM
      **CueExpander** seam may EXPAND THE CUE only (never select facts) — it is
      honest-unavailable in 3c.0 (no model call, deterministic fallback). Wired
      into the **live** path: `Coordinator.build_delegation_brief` uses `Curator`
      (boots in `runtime.py`) when present, stamping `policy_version` +
      graph-high-water; `MemoryBriefAssembler` stays the fallback for the bench.
      `memory_graph.RESERVED_FACT_TOPICS` keeps the ambient digest out of the
      general `current_facts` view (still re-derivable). Config: `curation_*`
      policy knobs. Tests: `test_curation.py` (30: cue building incl.
      alias/anaphora/graph-hop, every ranker feature + superseded-filter, budgeter
      digest/drop/floor in real tokens, ambient load/render/exclusion, miss/waste,
      expander honesty, Curator, TokenCounter determinism/fallback/stamp, and a
      byte-identical golden snapshot pinned to `POLICY_VERSION`). pytest 202/202.
- [x] **3c.0.2 Universal per-turn curation** — DONE (2026-06-12). Memory quality
      is now identical in chat and coding (Decision 13). `handle_utterance` routes
      EVERY chat turn (status / steering / general — everything except
      `coding_task`/`approval_response`/`stop`, which curate inside
      `build_delegation_brief`) through the same live `Curator` via the new
      `Coordinator._curate_chat_context` → shared `_curate_into_packet`
      (parameterized with `thread_id` override + `turn_kind`). Chat turns now get
      the curated ambient+cued brief injected into `packet.relevant_recall`, with
      receipts, and emit `curation.cue` + `curation.brief`/miss-waste events
      stamped `turn_kind="chat"` (so 3c.1 replay covers chat). `_handle_general`
      folds the curated brief into its reasoning input (no longer the 3-item
      `memory.recall`). `curate()` stays pure; the chat thread propagates into the
      cue so thread-affinity is live for chat threads. **Latency honesty:** the
      cued layer is recomputed live per turn (a small cost over the warm hot-cache
      fast path) so chat recall is never served stale — the warm cache's ambient
      slice still seeds the packet instantly. `MemoryBriefAssembler` untouched
      (bench fallback). Tests: `test_universal_curation.py` (7: chat
      ambient+cued, receipts, brief/cue events w/ turn_kind, determinism, live
      curator overrides stale warm cache, thread-affinity wired for chat, status
      turn curates). pytest 215/215.
- [x] **3c.1 Replay harness + quality-per-token bench + write-time embeddings** —
      DONE (2026-06-12). New module `centri/curation_replay.py` (kept beside
      `curation.py` so the golden-pinned read surface is untouched):
      `ReplayHarness` re-scores the recorded `curation.brief` ledger (partitioned
      chat vs delegation) and `quality_per_token()` is the headline metric
      (precision/recall of needed facts per token); `DigestBuilder` emits tiered
      daily→weekly digests as DERIVED VIEWS over the lossless spine (group live
      nodes by `created_at` window, deterministic, receipt-bearing; `DigestSummarizer`
      is the honest-unavailable LLM seam with a stable truncated-join fallback).
      Write-time embeddings: `EmbeddingProvider`/`NullEmbeddingProvider` (stamp
      `embedding:...`, honest-unavailable default), `vector` column added to
      `mem_decisions`/`mem_facts` (additive ALTER, JSON array), populated onto
      `Candidate.vector`, and an `embedding_similarity` ranker feature = pure
      `cosine_similarity` at read. **POLICY_VERSION stays `3c.0`**: the feature
      ships at weight **0.0** (config `curation_w_embedding_similarity`), so the
      brief render is byte-identical and the golden is unchanged — turning
      embeddings on is a deliberate future bump. Brief stamp gains
      `embedding_stamp`. pytest 233/233.
- [ ] **3d.1 Waking-up + spontaneous association** — the "feels human"
      proactivity track on 3c.0's machinery: waking-up situating brief on first
      interaction of a session/day, spontaneous association surfacing an
      unusually-high-scoring past item, and the open-loop scheduler (tick scans
      live open loops → `loop.nudge` per policy window → `/briefing`).
- [ ] **3e Continuity bench** — extend `core/src/centri/bench/` with
      unprompted-recall / supersession-churn / cold-start / delegated-work
      scenarios; wire as regression gate.

## State of the world (2026-06-12)

- **Done & pushed:** Phases 0–2 (event spine, ACP+OpenCode hands, memory graph
  w/ typed supersession, consolidation worker, cue-driven briefs, centri-bench
  native 1.00 vs Letta 0.93), shell UI v2 + glassmorphism, Phase 3a auth+deploy
  (`6e4bb2c`), 3b.1 full hand transcripts, 3b.2 threads (chat-scoped timeline,
  global memory), 3b.3 OpenCode ingestion adapter, 3b.4 memory bootstrap
  (ingest adapter registry + Claude Code/Cursor adapters + discovery +
  one-shot import), 3b.5 onboarding + single LLM config (OpenCode provider
  reuse + models.dev catalog + generic adapter + first-run import card),
  3c.0 deterministic context curation (pure `curate()` = ambient + cued layers,
  explicit-feature ranker, knapsack budgeter, miss/waste instrumentation, wired
  into the live `build_delegation_brief` path via `Curator`).
  Decisions ratified (shared-core continuity, OpenCode-via-ACP default,
  deterministic memory, north star, single-LLM-config, context-as-cache +
  deterministic-curation + no-visible-remembering/ambient) — see
  `docs/ROADMAP.md` → "Decisions". 3c.0.1 added real tiktoken (`o200k_base`)
  token budgeting behind a stamped `TokenCounter` (honest word-count fallback).
  **Phase A (2026-06-12):** `docs/VISION.md` end-to-end master plan; four ratified
  structural decisions (tenancy key / voice transport / tool abstraction / TEMPR
  retrieval, #9–12 above); tenancy-key migration (`tenant_id` default `"local"`
  on events + graph tables, threaded through the query layer, additive ALTER
  preserves existing DBs); `VERIFY.md` owner real-machine checklist (Phase 0 gate).
  pytest 208/208, vitest 14/14, tsc/build clean. **Decision 13 ratified
  (2026-06-12):** "Photographic storage, human recall" — spine photographic
  (append-only, lossless, re-derivable), recall human (gist-first curated briefs
  w/ receipt-backed zoom); forgetting is a read-time policy, never write-time
  deletion; 3c.1 digests are derived views. Work item **3c.0.2 universal per-turn
  curation** inserted ahead of 3c.1. **3c.0.2 DONE:** chat turns now flow through
  the same live `curate()` Curator path as coding delegation
  (`Coordinator._curate_chat_context`), with receipts + `curation.brief`/miss-waste
  events stamped `turn_kind="chat"`; `_handle_general` reasons over the curated
  brief, not `memory.recall`. pytest 215/215. **3c.1 DONE (2026-06-12):**
  `centri/curation_replay.py` adds the replay harness (`ReplayHarness` re-scores
  the recorded `curation.brief` ledger, chat/delegation partitioned),
  quality-per-token bench (`quality_per_token` = F1-of-needed-facts / tokens),
  tiered daily→weekly digests (`DigestBuilder`, derived views over the lossless
  spine, receipt-bearing, deterministic; LLM summarizer seam honest-unavailable),
  and write-time embeddings (`EmbeddingProvider` honest-unavailable, `vector`
  column on `mem_decisions`/`mem_facts`, `embedding_similarity` ranker feature =
  pure cosine). Embedding weight defaults to 0.0 so POLICY_VERSION stays `3c.0`
  and the golden brief is byte-identical. pytest 233/233.
- **Next:** Phase 2 (feels human / was 3d) — prose ambient narrative, waking-up
  briefing, spontaneous association, dormancy nudges — on the now-complete
  Phase-1 machinery. Optional 3c.1 follow-ons: a network-backed embedding
  provider behind `resolve_embedding_provider` (then bump POLICY_VERSION + new
  golden when weight goes positive); Graphiti/Hindsight external baselines.
- **Layout:** `core/` Python FastAPI (src/centri/: app.py, db.py, coordinator,
  consolidation, memory_graph, memory_brief, curation, curation_replay, briefing, opencode_config,
  models_catalog, model_router, hands/, ingest/ [base + registry +
  opencode/claude_code/cursor/generic adapters], bench/);
  `shell/` React+TS+Tailwind (+ Tauri scaffold; components/OnboardingCard);
  `deploy/` VM bundle; `docs/` architecture/roadmap/vision/memory/bench/
  event-contract/ingestion-adapters; `VERIFY.md` (repo root) = Phase 0 checklist.
- **Run locally (sandbox):** backend
  `env LITELLM_BASE_URL=http://127.0.0.1:4999/v1 LITELLM_API_KEY=test-proxy-key CENTRI_AUTONOMY_LEVEL=supervised python -m uvicorn centri.app:app --host 127.0.0.1 --port 8787`
  from `core/` (fake proxy URL → all role models report configured; supervised →
  approval cards). Shell: `npm run dev -- --host 127.0.0.1 --port 1420` from
  `shell/`; set backend URL (and auth token if set) in Settings. DB:
  `~/.centri/state.db` — note pytest writes to it (pollutes dev timeline).
- **Auth:** set `CENTRI_AUTH_TOKEN` → Bearer on REST (except `/health`),
  `?token=` on WS. Empty = disabled (dev).

## Contracts you must not break

- vitest pins UI text: "No activity yet", exact "Approve"/"Reject" labels,
  "{risk} risk", /approved/i on resolved cards, raw event type text visible
  (e.g. "hand.started"), task progress summaries visible while running.
- Backend event envelope: stable `id` from DB, `type`, `source`, `ts`,
  `thread_id`, `task_id`, payload normalized by `_normalize_event_row`
  (`/events` must match live WS shape; oldest-first after client reverse).
- Events are append-only; memory must stay re-derivable
  (`rebuild_from_events()`); redaction before persistence.
- Approval routes are idempotent; resolved decision lives in `payload.decision`
  AND `action`.
- Threads scope the *chat timeline only* — `/briefing` and `/memory/graph` must
  stay unscoped (one memory across all threads). `user.utterance` /
  `coordinator.response` carry `thread_id`; the shell shows frames matching the
  active thread plus any frame with no `thread_id` (global). Coding tasks still
  spin their own work-thread per task — separate from the chat thread.
- `curate()` is pure: NO wall-clock, NO randomness, NO LLM at read time. Same
  `(graph_snapshot, cue, budget, policy_version)` must render a byte-identical
  brief (the golden snapshot pins this). Recency is a TIEBREAK ONLY via the
  stored-timestamp ordinal, never `now()`. Bump `POLICY_VERSION` (and add a new
  golden) for any deliberate brief-shape change. The cue-expander seam may only
  EXPAND THE CUE (add query terms), never select facts.
- The budgeter measures items in REAL tokens via a `TokenCounter` (default =
  pinned tiktoken `o200k_base`, stamped `tiktoken:o200k_base`; honest
  `wordcount:v1` fallback only when tiktoken is unavailable). The active
  counter's `stamp` rides on every brief (`CuratedBrief.tokenizer_stamp`, in
  `curation_breakdown_payload`) and IS part of the policy identity: changing the
  tokenizer/encoding must bump the stamp (and re-pin any affected golden). The
  fallback path is never silent — its stamp says `wordcount:v1`.
- 3c.1 write-time embeddings: the `embedding_similarity` ranker feature ships at
  weight **0.0** (config `curation_w_embedding_similarity`) so POLICY_VERSION
  stays `3c.0` and the golden brief is byte-identical — the feature is computed +
  shown in the breakdown for explainability but cannot move a score. Turning it
  on (positive weight + a real provider behind `resolve_embedding_provider`) is a
  deliberate `POLICY_VERSION` bump + new golden. Embeddings are computed at WRITE
  time only; read time is pure `cosine_similarity` over stored `Candidate.vector`
  (no model call) so `curate()` purity holds. The provider `stamp`
  (`embedding:unavailable` by default) rides on `CuratedBrief.embedding_stamp` and
  in `curation_breakdown_payload`, like the tokenizer stamp. `mem_decisions`/
  `mem_facts` carry a nullable `vector` (JSON array, additive ALTER); open loops
  do not.
- The ambient digest is a reserved Fact (`ambient-standing-context`, tag
  `ambient`) excluded from the general `current_facts` view via
  `RESERVED_FACT_TOPICS` — keep it out of the cued candidate set and out of any
  exact `current_facts` count assertions; read it with `include_reserved=True`.
- `Coordinator.build_delegation_brief` uses `Curator` (the live `curate()` path)
  when wired; `MemoryBriefAssembler` is the fallback the bench still uses — do
  not delete it.
- 3c.0.2: chat turns (status/steering/general) curate through the SAME live
  `Curator` as coding delegation via `_curate_chat_context` → `_curate_into_packet`.
  Curation events carry `turn_kind` ("chat"|"delegation") — keep it on the
  payload (the 3c.1 replay harness partitions on it). A coding turn must emit
  exactly ONE `curation.brief` (delegation-side); do not also chat-curate
  `coding_task`/`approval_response` turns or you double-count.

## Known traps

- Vite HMR serves a stale module graph after hook edits →
  `rm -rf node_modules/.vite` and restart dev server with `--force`.
- "please refactor the auth module" triggers the coding-intent heuristic
  (approval card); "update the README" does not.
- Settings hands list shows per-capability rows (duplicate-looking) — known
  cosmetic, leave it.
- `core_token` in config.py is legacy/unused; auth uses `auth_token`.
- 3c.0 `curation.miss`/`curation.waste` are emitted at brief time scored against
  the *cue* as the stand-in "turn text". 3c.1's `ReplayHarness` re-scores the
  recorded `curation.brief` ledger (it reads `lines`/`miss_count`/`waste_count`/
  `turn_kind` off the payload), so keep those fields on the event — the harness
  is a pure re-scoring of recorded turns, not a re-`curate()`.
