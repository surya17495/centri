# HANDOFF — read this first if you are a fresh agent

Owner: surya (surya.munna95@gmail.com). Repo: https://github.com/surya17495/centri

## Non-negotiable working rules

1. **Commit and `git push origin main` after every completed piece** (~30–60 min
   of work). Pushed code is the only safe code; the session may die any time.
   Git identity: `-c user.name="surya17495" -c user.email="surya.munna95@gmail.com"`.
2. **Quality gates before every push:** `cd core && python -m pytest tests/ -q`
   (currently 251 passed) and, if `shell/` was touched,
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

Decision (ratified 2026-06-11 PT, owner — north star v2):

14. **North star v2 — "reasoning partner."** CENTRI thinks like a human with
    machine superpowers (photographic memory bandwidth, tool use inside a VM,
    voice). **Conversational seamlessness is first-class now**, no longer
    deferred behind the coding wedge; coding stays the first hand + distribution
    wedge, but "feels human in conversation" items (waking-up, spontaneous
    association, prose narrative, nudges) are **no longer "premature Jarvis."**
    Supersedes Decision 4's scope test. **New scope test:** *does this make
    memory+conversation more seamless, or the partner more capable, with
    receipts — or is it polish?* Decisions 1–13 stay intact (determinism,
    receipts, photographic storage / human recall all still bind).

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
- [x] **3c.1-embed Semantic leg ON (Unit 2)** — DONE (2026-06-12). Turned the
      embedding leg on behind explicit config WITHOUT breaking the 3c.0 golden.
      New `POLICY_VERSION_EMBED = "3c.1-embed"` selected purely by
      `active_policy_version(weights)` (positive `embedding_similarity` →
      embed policy; weight 0.0 → unchanged `3c.0`). Real providers behind the
      existing seam: `LocalEmbeddingProvider` (lazy fastembed, pinned model,
      stamp `embedding:local:<model>`), `LiteLLMEmbeddingProvider` (reuses the
      ModelRouter / existing key resolution, stamp `embedding:litellm:<model>`),
      and `HashingEmbeddingProvider` (clearly-labeled deterministic BENCH stub,
      stamp `embedding:hashing-stub:dN`). `resolve_embedding_provider` preference
      = local → litellm (when `embedding_enabled` + model) → Null (offline
      default, keeps CI green). Config: `CENTRI_EMBEDDING_{ENABLED,LOCAL_MODEL,MODEL}`.
      Write-time embedding + idempotent backfill: `Consolidator` embeds
      `topic: statement` at write (null provider → no vector); `backfill_embeddings()`
      computes vectors for live nodes lacking one (idempotent via `vector is None`
      guard, re-writes through INSERT-OR-REPLACE `add_*`), emits
      `embedding.backfill.{started,progress,completed}` on the spine;
      honest-unavailable reports `embedded=0`. POST `/memory/embeddings/backfill`
      exposes it. New golden `GOLDEN_EMBED` pinned to `3c.1-embed` (positive
      weight + stub provider, stamp `embedding:hashing-stub:d256`), byte-stable
      like the base golden. Paraphrase bench
      `python -m centri.bench.paraphrase_embed` (offline, hashing-stub):
      zero-lexical-overlap cues under a one-line budget, OFF→ON quality-per-token
      recorded honestly in `docs/centri-bench.md` (surfaced rate 0.0→1.0, recall
      0.0→1.0, qpt 0.0→0.0939, misses 3→0). Honesty: real local/LiteLLM embedding
      models are *seam-verified only* (fastembed needs onnxruntime + a model
      download; LiteLLM needs network) — the bench + golden use the labeled stub
      so the suite stays offline. Tests: `test_embeddings.py` (17),
      `test_centri.py::TestEmbeddingBackfill` (1). pytest 251/251.
- [x] **A1 Real `opencode acp` binary verification** — DONE (2026-06-12).
      Installed the real binary in-sandbox (`npm i -g opencode-ai`, v1.17.4) and
      drove `AcpHand` through the full ACP lifecycle
      (`initialize → session/new → session/prompt`) against it. **It works in
      this sandbox** — opencode's default provider resolved a model with no key,
      streamed a real turn, and returned `stopReason=end_turn`; the hand recorded
      an honest `hand.transcript`+`hand.completed` trail with a receipt.
      **Divergences from `acp_fake_agent.py` (all handled gracefully, no
      crash/hang):** the real binary emits `agent_thought_chunk` (reasoning),
      `usage_update`, and `available_commands_update` session/update kinds the
      fake never did, and `session/new` returns rich `configOptions` /
      `agentCapabilities` (`loadSession`, `sessionCapabilities`,
      `mcpCapabilities`). **Fix applied:** `AcpHand` now captures
      `agent_thought_chunk` into a new transcript `reasoning` field for fidelity
      (never leaked into the user-facing summary or fact); the other unknown
      kinds are correctly ignored. Tests:
      `test_acp_hand.py::test_real_opencode_acp_lifecycle`
      (`@pytest.mark.skipif(shutil.which("opencode") is None)` — RAN, not skipped,
      in this sandbox) and `::test_realish_update_kinds_are_handled` (deterministic
      via `ACP_FAKE_REALISH=1`, pins the behavior when the binary is absent).
      pytest 253/253.
- [x] **A2 ACP conformance + error-path hardening** — DONE (2026-06-12).
      Extended `acp_fake_agent.py` with adversarial modes and added 7 tests in
      `test_acp_hand.py`: malformed JSON-RPC frame (skipped, turn still
      completes), agent crash mid-turn (fails honestly, no fake transcript, no
      orphaned connection), hung agent (per-turn timeout fires → failed), oversized
      2 MiB chunk, permission-request timeout (gate raises → deny, turn ends),
      cancellation mid-stream (→ cancelled), and restart after agent exit (same
      hand reused for a clean turn). **Real bug found + fixed:** asyncio's
      StreamReader defaults to a 64 KiB line limit and raised "Separator is not
      found, and chunk exceed the limit" on a large frame — the hand would have
      broken on a legitimate big message/tool result from a real agent. Fixed by
      passing `limit=16 MiB` to `create_subprocess_exec`. Also added an injectable
      `prompt_timeout` to `AcpHand` so a hung agent is bounded (was a hard-coded
      600 s). Every error path leaves the hand recoverable (connection popped from
      the registry) and emits honest events. pytest 260/260.
- [x] **A3 Failover drill (e2e)** — DONE (2026-06-12). New `test_failover.py`
      drives the full `Jobs` + `Hands` + `Database` stack: ACP healthy → task
      delegated → ACP process dies mid-task → router degrades to the OpenCode
      fallback → task ends `completed` with an honest `hand.degraded` event
      (names `failed_hand=acp`, `failed_status`, `fallback_hand=opencode`) and
      NO orphaned task left `running`. Second test: when the fallback is
      honest-unavailable, the task fails honestly (no fake success), trail intact,
      no orphan. **Behavior change (intentional):** `Hands.execute` previously did
      selection-time failover only (pick the healthy hand up front, run it, return
      its result even on mid-task failure). It now degrades down the priority chain
      *at run time* — on a non-success status OR a raised exception it tries the
      next advertiser, emitting `hand.degraded` per step; the last honest failure
      is returned when the chain is exhausted (never a faked success). `select()`
      is unchanged in meaning (returns the preferred reachable hand) but is now a
      thin wrapper over `_advertisers()`. `Jobs._run_job` already guarantees a
      terminal task state on every path, so "no orphaned running task" holds
      structurally. pytest 262/262.
- [x] **B1 3c.0.2 chat/coding curation parity — VERIFIED + bug fixed** (2026-06-12).
      Audited the suspects (`coordinator.py` ~268/325 `memory.recall(text, limit=3)`
      and `context.py`): those are only the hot-cache/cold *seed*; every non-coding
      chat turn then runs `_curate_chat_context` → the SAME `_curate_into_packet`
      → same `Curator.assemble()` → same pure `curate()` as
      `build_delegation_brief`, appending the curated brief on top of (and not
      replacing) the seed. So 3c.0.2 parity is genuinely implemented. Added
      `test_curation_parity.py` (6) proving it structurally: chat and delegation
      render a **byte-identical** brief for the same `(graph, cue, budget,
      policy)`; both carry matching policy identity (`policy_version` /
      `tokenizer_stamp` / `embedding_stamp`) differing only in `turn_kind`; both
      carry `source_event_id` receipts; a coding turn emits **exactly one**
      `curation.brief` (`turn_kind=delegation`, no chat double-count) and a chat
      turn exactly one (`turn_kind=chat`). **Real bug found + fixed:**
      `build_delegation_brief` returned `self._finish_brief(...)` WITHOUT `await`
      on the curator-injected path (line 564) — the live coding path handed a
      *coroutine object* to the hand as `user_intent` instead of the rendered
      brief. Now awaited; pinned by
      `test_delegation_brief_returns_a_string_not_a_coroutine`. pytest 268/268.
- [x] **B2 3e continuity bench scaffold** — DONE (2026-06-12). Added
      `core/src/centri/bench/continuity.py` with the four 3e failure-mode suites
      motivated by the owner's Hermes failures: (1) `cross_session_awareness`
      (a prior-session open loop surfaces *unprompted* at a cold cue that does
      not lexically name it), (2) `supersession_under_churn` (a fact renamed
      thrice resolves to the LATEST value only; every stale value absent),
      (3) `cold_start_recall` (fresh DB+graph, memory rebuilt purely via
      `rebuild_from_events`, brief still carries decisions/conventions —
      re-derivability, no warm cache), (4) `delegated_session_awareness` (typed
      hints from a delegated hand's `task.completed` surface next brief). Each
      suite scores a real pass/fail via the SAME production cold-start path
      (`Consolidator.rebuild_from_events` → `MemoryBriefAssembler.assemble`),
      reported honestly by `report_continuity`. Wired into
      `python -m centri.bench.run --suite continuity` (+ `--json`). **Personas
      are STUBS** — one `ContinuityPersona` (`_hermes_like`) shaped to exercise
      all four modes, explicitly `# TODO(owner): replace with real Hermes
      transcript material` before the numbers are quoted as 3e evidence. The
      *methodology* is real and under test (`test_continuity.py`, 7 tests:
      gate runs one score per (suite,persona), scores are real booleans with a
      failure detail, deterministic, cold-start brief rebuilt purely from
      events, plus two suite-validity guards). On the stub persona all four
      suites PASS (1.00) — expected, since the placeholder ground truth sits
      within CENTRI's known-good path; **failures are 3e FINDINGS, not bugs to
      hide** (anti-gaming rule), and emerge once real Hermes material is seeded.
      pytest 275/275.
- [x] **3c.2 Temporal narrative** — DONE (2026-06-12). "what changed since X" +
      "where did we leave off", a DERIVED VIEW over the photographic spine +
      bi-temporal graph (Decision 13). **Slice 1:** `centri/temporal.py`
      (`TemporalNarrator`, beside `curation_replay.py` so the golden read-surface
      is untouched). `changed_since(anchor)` diffs the live graph against an ISO
      anchor — additions (created after), supersessions (invalidated after,
      rendered old→new with the NEW value's receipt), open-loop status changes
      (new/revisited/completed/parked); an in-window supersession suppresses the
      target's standalone "added" line (narrate the change once). `where_left_off()`
      = resume view: anchors on the last real activity event (skips derived
      `curation.*`/`memory.synthesized`), surfaces still-open loops + latest
      decisions + the last event, every line receipted. `resolve_anchor` accepts
      ISO date / full ISO / `last-session` (idle-gap scan on the spine) / origin.
      **Purity:** ISO sorts lexically so the diff is pure string compare — no
      `now()`, no calendar lib, no LLM; same `(graph, anchor)` → byte-identical
      render. **Slice 2 DONE (2026-06-12):** wired into `runtime`
      (`runtime.temporal_narrator`, None pre-boot like `proactive_brief`) and two
      read-only endpoints — `GET /memory/since?since=<iso|date|last-session|>`
      (resolves the anchor then narrates) and `GET /memory/where-left-off`. Both
      stay unscoped like `/memory/graph` (one memory across threads), accept an
      optional `repo_id`, return `{available, query, anchor, anchor_kind, lines,
      text}`. **Slice 3 DONE (2026-06-12):** chat-intent routing — a new
      `temporal` intent in `Coordinator._classify_intent` (checked BEFORE the
      coding/status heuristics so "what changed"/"add" overlap doesn't mis-route)
      detects resume phrasings ("where did we leave off", "catch me up", …) and
      "what changed since <anchor>" via pure module-level matchers
      (`_is_temporal_query`/`_is_resume_query`/`_extract_since` — ISO date /
      'last session' / origin). `_handle_temporal` renders the narrator's view
      (resume → `where_left_off`, else `changed_since`), emits a
      `coordinator.temporal` event, and returns `response_type="temporal"` with
      `{query, anchor, anchor_kind, lines}`. The turn is chat-curated like any
      other non-coding turn (NOT in the curation skip-list), so it still emits one
      `turn_kind="chat"` `curation.brief`. Honest-unavailable: if no narrator is
      wired, the intent never fires (falls through to general). Tests:
      `test_temporal.py` (20: 16 core + 4 intent-matcher), `test_centri.py::TestTemporal`
      (5: 2 endpoint shape + 1 where-left-off + 2 chat-routing). pytest 299/299.
- [ ] **Phase 4 / Tool contract (Decision 11)** — IN PROGRESS (2026-06-12).
      Tools = first-class contract parallel to Hand: every invocation is an event
      with receipts; side-effectful tools round-trip the existing approval gate;
      output folds into memory via a consolidation fact hint. Composio is the
      first provider, Tavily search the demo tool.
      - [x] **Step 1 — Tool contract core** — DONE (2026-06-12). New package
            `core/src/centri/tools/` (`base.py`): `ToolSpec` / `ToolResult` /
            `ToolProvider` ABC (honest `available()` + reason) and `ToolRegistry`.
            `registry.invoke()` is the ONLY execution path: emits `tool.requested`
            (via `db.append_event` so redaction runs + tenant_id carried), gates
            side-effectful tools through the SAME approval-gate callable hands use
            (deny/timeout/no-gate => `tool.denied`, honest failure, no execution;
            read-only slugs skip the gate via `is_read_only_slug`), executes via the
            provider, then emits `tool.completed`/`tool.failed` with a 240-char
            summary + full output and a deterministic `fact` hint
            (`topic: tool:<provider>:<name>`, tags `[tool, <provider>]`) that
            consolidation folds into the graph with a receipt (mirrors
            `hand.transcript`). Zero providers / unknown tool / unavailable provider
            are all honest-unavailable, never faked. Tests: `test_tools.py` (13:
            read-only classification, zero-provider honest-unavailable, unavailable
            provider never executes, read-only skips gate, side-effect deny/no-gate
            blocks execution, allow executes, provider failure => tool.failed, fact
            hint folds into graph w/ receipt, failed invocation writes no fact,
            secret in output redacted before persistence). pytest 347/348 (1 skip).
      - [ ] **Step 2 — Composio provider** (`tools/composio.py`, mocked-HTTP tests).
      - [ ] **Step 3 — API + coordinator wiring** (`GET /tools`, `POST /tools/invoke`).
- [ ] **3d.1 Waking-up + spontaneous association** — the "feels human"
      proactivity track on 3c.0's machinery: waking-up situating brief on first
      interaction of a session/day, spontaneous association surfacing an
      unusually-high-scoring past item, and the open-loop scheduler (tick scans
      live open loops → `loop.nudge` per policy window → `/briefing`).
- [x] **3e Continuity bench (scaffold)** — DONE as **B2** above
      (`bench/continuity.py` + `--suite continuity` + `test_continuity.py`).
      The unprompted-recall / supersession-churn / cold-start / delegated-work
      suites and the regression gate exist; personas remain
      `# TODO(owner)` stubs awaiting real Hermes transcript material.

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
  and the golden brief is byte-identical. pytest 233/233. **Unit 2 / 3c.1-embed
  DONE (2026-06-12):** semantic leg turned ON behind explicit config without
  breaking the 3c.0 golden — `POLICY_VERSION_EMBED = "3c.1-embed"` selected by
  `active_policy_version(weights)` (positive `embedding_similarity` → embed
  policy + its own `GOLDEN_EMBED`; weight 0.0 → unchanged `3c.0`). Real providers
  behind the seam (`LocalEmbeddingProvider` fastembed, `LiteLLMEmbeddingProvider`
  reusing existing key resolution, `HashingEmbeddingProvider` labeled bench stub),
  `resolve_embedding_provider` = local→litellm→Null (offline default). Write-time
  embedding in consolidation + idempotent `backfill_embeddings()` (POST
  `/memory/embeddings/backfill`, `embedding.backfill.*` spine events,
  honest-unavailable reports `embedded=0`). Offline paraphrase bench
  (`centri.bench.paraphrase_embed`, hashing-stub) records OFF→ON quality-per-token
  in `docs/centri-bench.md` (surfaced 0.0→1.0, recall 0.0→1.0, qpt 0.0→0.0939,
  misses 3→0). Real local/LiteLLM models are seam-verified only (need
  onnxruntime+model download / network); bench + golden use the labeled stub.
  pytest 251/251.
- **Testing-hardening pass (2026-06-12, pre-VERIFY.md):** A1 real `opencode acp`
  binary verification DONE — the real binary (v1.17.4) was installed and drove
  `AcpHand` through a full lifecycle in-sandbox; protocol-compatible, divergences
  documented + the reasoning-chunk fidelity gap fixed. This flips VERIFY.md
  step 3 ("real ACP coding task") from purely real-machine-pending to
  **sandbox-verified** (a real model resolved here without a key); a real-machine
  pass on the owner's own provider config is still the final confirmation.
  A2 error-path hardening DONE — 7 adversarial ACP modes (malformed frame,
  mid-turn crash, hung/timeout, oversized chunk, permission timeout,
  cancellation race, restart) all produce honest events + recoverable state;
  found+fixed a real 64 KiB StreamReader line-limit bug (large frames would
  break the hand) by raising the subprocess limit to 16 MiB. A3 failover drill
  DONE — `test_failover.py` kills ACP mid-task and proves run-time degradation
  to the OpenCode fallback (honest `hand.degraded` trail naming the failed hand,
  no orphaned running task; honest failure when no fallback remains);
  `Hands.execute` rewritten to iterate advertisers and degrade on any
  non-success status or raise. B1 curation parity VERIFIED + a missing-`await`
  bug in `build_delegation_brief` found+fixed (the live coding path handed a
  coroutine to the hand as its intent). B2 3e continuity gate SCAFFOLDED
  (`bench/continuity.py` + `--suite continuity`, four Hermes-failure suites,
  `# TODO(owner)` stub personas, methodology under test). All sandbox-verified;
  the only real-machine-pending item remaining is the owner's own-provider ACP
  confirmation (VERIFY.md step 3) and the deploy/Tauri toolchain items.
  pytest 275/275.
- **3c.2 temporal narrative DONE (2026-06-12):** `centri/temporal.py`
  `TemporalNarrator` — a pure, receipt-bearing derived view over the spine +
  bi-temporal graph answering "what changed since X" (`changed_since`: additions,
  supersessions old→new, open-loop status changes after an ISO anchor) and "where
  did we leave off" (`where_left_off`: still-open loops + latest decisions + last
  real activity). `resolve_anchor` handles ISO date / full ISO / `last-session`
  idle-gap scan / origin. Read-only endpoints `GET /memory/since` +
  `GET /memory/where-left-off` (unscoped like `/memory/graph`). Chat routing: a
  `temporal` intent (matched before coding/status, chat-curated, honest-unavailable
  without a narrator) → `coordinator.temporal` event + `response_type="temporal"`.
  Same `(graph, anchor)` → byte-identical render (no `now()`/calendar/LLM at read
  time). Tests: `test_temporal.py` (20), `test_centri.py::TestTemporal` (5).
  pytest 299/299.
- **Phase 4 / Tool contract Step 1 DONE (2026-06-12):** `centri/tools/`
  (`base.py`) lands the first-class tool contract (Decision 11). `ToolRegistry.invoke`
  is the single execution path — `tool.requested` → (approval gate for
  side-effectful tools) → provider exec → `tool.completed`/`tool.failed`/`tool.denied`,
  all written through `db.append_event` (redaction + tenant_id). The completion
  event carries a deterministic `fact` hint (`tool:<provider>:<name>`, tags
  `[tool, <provider>]`) folded by consolidation with a receipt, mirroring
  `hand.transcript`. Read-only slugs (SEARCH/GET/LIST/FETCH/…) skip the gate;
  everything else is conservatively side-effectful. Honest-unavailable throughout
  (zero providers, unknown tool, no-key provider — never faked). New contract: tool
  events on the spine, approval-gated side effects, fact-hint folding. Composio
  provider (Step 2) + REST endpoints (Step 3) next. pytest 347/348 (1 skip).
- **North star v2 (Decision 14, ratified 2026-06-11 PT):** CENTRI is a
  **reasoning partner** — conversational seamlessness first-class, thinks like a
  human with machine superpowers (memory bandwidth, VM tool use, voice). Docs
  updated (ROADMAP/VISION/HANDOFF Decisions + scope test). The "feels human"
  conversation items are no longer "premature Jarvis."
- **Docker self-host slice DONE (2026-06-12):** minimal container boot for the
  Oracle Cloud Always Free VM (Ampere A1, arm64). `core/Dockerfile`
  (plain `python:3.11-slim`, multi-arch arm64+amd64, `pip install .` → `centri`
  uvicorn entry, binds `0.0.0.0:8760`, `/data/state.db`, curl `/health`
  HEALTHCHECK), `shell/Dockerfile` (node build → nginx static SPA, backend URL is
  a runtime Settings value so nothing is baked in), `docker-compose.yml` at root
  (`server` :8760 + named `centri-data` volume for the memory spine, `shell`
  :8761, `restart: unless-stopped`, `env_file: .env`), `.dockerignore` per image
  (keeps `.env`/`*.db`/tests out), `.env.example` extended with a Docker/BYOK note
  + `CENTRI_AUTH_TOKEN`, and `docs/DEPLOY.md` (Oracle-VM quickstart: install
  docker → clone → cp .env.example .env → fill keys → `docker compose up -d` →
  open firewall 8760/8761 → curl `/health`). **Verification (honest):**
  compose/Dockerfiles written, **sandbox-verified config only** (`docker
  compose config`-equivalent YAML parse + build-context presence checks pass;
  docker is NOT installed in this sandbox so NO image build or container boot was
  run) — **boot verification pending on the user's VM.** pytest gate unchanged
  (299 passed / 1 skipped); no core Python or shell app code touched.
  **Next: boot on VM** checklist for the owner:
  1. `sudo apt-get install -y docker.io docker-compose-v2 && sudo systemctl enable --now docker`
  2. `git clone … && cd centri && cp .env.example .env` then fill `LITELLM_*`/provider keys + `CENTRI_AUTH_TOKEN`
  3. `docker compose up -d && docker compose ps`
  4. open Oracle Security List + host iptables for TCP 8760 & 8761 (see docs/DEPLOY.md)
  5. `curl -fsS http://127.0.0.1:8760/health` → `{"status":"ok"}`, then open `http://<VM_IP>:8761`, set Backend URL + Auth token in Settings
  6. if the build is slow on the A1, it's the shell `npm ci`/`vite build` stage — server image is small
- **Next:** Opinions-with-confidence (Unit 3) — a typed agent-stance memory class
  (confidence + receipts, supersession, own type prior, rendered as stances in
  briefs, deterministic consolidation only). Then Phase 2 (feels human / was 3d) —
  prose ambient narrative, waking-up briefing, spontaneous association, dormancy
  nudges — on the now-complete Phase-1 machinery. Voice (Phase 5) + VM tool use
  (Phase 4) are the following rounds on the now-accelerated trajectory (need a
  real machine / approval-gated tool contract). Optional Phase-1 follow-ons:
  Graphiti/Hindsight external baselines.
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
- Write-time embeddings + the two-policy split (3c.1 → Unit 2): the
  `embedding_similarity` ranker feature is read-time pure `cosine_similarity`
  over stored `Candidate.vector` (no model call) so `curate()` purity holds. The
  active policy is chosen by `active_policy_version(weights)`: weight **0.0** →
  `POLICY_VERSION = "3c.0"` (default; golden in `test_curation.py` stays
  byte-identical), positive weight → `POLICY_VERSION_EMBED = "3c.1-embed"` (its
  own golden `GOLDEN_EMBED` in `test_embeddings.py`). **Both goldens are
  load-bearing — do not break either.** A deliberate brief-shape change under a
  positive weight must re-pin `GOLDEN_EMBED`; under weight 0.0 must re-pin the
  base golden. Enabling embeddings (a real provider via
  `resolve_embedding_provider` + a positive `curation_w_embedding_similarity`) is
  therefore not a silent change — it lands a node on the `3c.1-embed` policy with
  its own pinned render. Embeddings are computed at WRITE time only (consolidation
  `_embed`, and `backfill_embeddings()` idempotently for pre-existing nodes — the
  `vector is None` read is the idempotency guard). The provider `stamp`
  (`embedding:unavailable` by default) rides on `CuratedBrief.embedding_stamp` and
  in `curation_breakdown_payload`, like the tokenizer stamp; it is part of the
  policy identity (the `3c.1-embed` golden pins `embedding:hashing-stub:d256`).
  `resolve_embedding_provider` stays honest-unavailable (Null) unless
  `CENTRI_EMBEDDING_*` config selects a local/litellm provider — nothing turns on
  by accident. `mem_decisions`/`mem_facts` carry a nullable `vector` (JSON array,
  additive ALTER); open loops do not.
- The ambient digest is a reserved Fact (`ambient-standing-context`, tag
  `ambient`) excluded from the general `current_facts` view via
  `RESERVED_FACT_TOPICS` — keep it out of the cued candidate set and out of any
  exact `current_facts` count assertions; read it with `include_reserved=True`.
- `Coordinator.build_delegation_brief` uses `Curator` (the live `curate()` path)
  when wired; `MemoryBriefAssembler` is the fallback the bench still uses — do
  not delete it.
- `Hands.execute` degrades down the priority chain (`_advertisers`: healthy
  first, then unhealthy, in `hand_priority` order). On a non-success status
  (anything outside `completed/ok/steered/cancelled`) OR a raised exception it
  tries the next advertiser and emits a `hand.degraded` event
  (`failed_hand`/`failed_status`/`reason`/`fallback_hand`). It NEVER fakes a
  success: when the chain is exhausted the last honest failure is returned.
  `Jobs._run_job` always writes a terminal task state, so no task is left
  `running` — keep both invariants (`test_failover.py` pins them).
- 3c.0.2: chat turns (status/steering/general) curate through the SAME live
  `Curator` as coding delegation via `_curate_chat_context` → `_curate_into_packet`.
  Curation events carry `turn_kind` ("chat"|"delegation") — keep it on the
  payload (the 3c.1 replay harness partitions on it). A coding turn must emit
  exactly ONE `curation.brief` (delegation-side); do not also chat-curate
  `coding_task`/`approval_response` turns or you double-count.
- 3c.2: `centri/temporal.py` `TemporalNarrator` is a PURE derived view — same
  `(graph, resolved-anchor)` must render a byte-identical narrative (ISO sorts
  lexically, so the diff is string comparison; NO `now()`, NO calendar lib, NO
  LLM at read time, NO confabulation — every line carries a `source_event_id`
  receipt). An in-window supersession SUPPRESSES the new value's standalone
  "added" line (narrate the change once, old→new, receipt = the new live value).
  `/memory/since` + `/memory/where-left-off` stay UNSCOPED like `/memory/graph`.
  The `temporal` chat intent is matched BEFORE the coding/status heuristics in
  `_classify_intent`; it is chat-curated (one `turn_kind="chat"` brief), NOT in
  the curation skip-list. Honest-unavailable when no narrator is wired (intent
  never fires). The anchor resolver treats a bare `YYYY-MM-DD` as start-of-day
  UTC and `last-session` as the most-recent idle-gap boundary on the spine.

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
