# HANDOFF — read this first if you are a fresh agent

Owner: surya (surya.munna95@gmail.com). Repo: https://github.com/surya17495/centri

## Non-negotiable working rules

1. **Commit and `git push origin main` after every completed piece** (~30–60 min
   of work). Pushed code is the only safe code; the session may die any time.
   Git identity: `-c user.name="surya17495" -c user.email="surya.munna95@gmail.com"`.
2. **Quality gates before every push:** `cd core && python -m pytest tests/ -q`
   (currently 112 passed) and, if `shell/` was touched,
   `cd shell && npx tsc --noEmit && npx vitest run && npx vite build` (9 tests).
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
- [ ] **3d.1 Open-loop scheduler** — scheduler tick scans open loops, emits
      one-time `loop.nudge` events per policy window; surfaces in `/briefing`.
- [ ] **3c.1 Tiered consolidation** — daily/weekly digests + entity pages;
      brief reads derived layers only.
- [ ] **3e Continuity bench** — extend `core/src/centri/bench/` with
      unprompted-recall / supersession-churn / cold-start / delegated-work
      scenarios; wire as regression gate.

## State of the world (2026-06-11)

- **Done & pushed:** Phases 0–2 (event spine, ACP+OpenCode hands, memory graph
  w/ typed supersession, consolidation worker, cue-driven briefs, centri-bench
  native 1.00 vs Letta 0.93), shell UI v2 + glassmorphism, Phase 3a auth+deploy
  (`6e4bb2c`), 3b.1 full hand transcripts, 3b.2 threads (chat-scoped timeline,
  global memory), 3b.3 OpenCode ingestion adapter. Decisions ratified
  (shared-core continuity, OpenCode-via-ACP default, deterministic memory) —
  see `docs/ROADMAP.md` → "Decisions". pytest 125/125, vitest 9/9,
  tsc/build clean.
- **Next:** 3d.1 open-loop scheduler (per the work queue order below).
- **Layout:** `core/` Python FastAPI (src/centri/: app.py, db.py, coordinator,
  consolidation, memory_graph, memory_brief, briefing, hands/, ingest/, bench/);
  `shell/` React+TS+Tailwind (+ Tauri scaffold); `deploy/` VM bundle;
  `docs/` architecture/roadmap/memory/bench/event-contract.
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

## Known traps

- Vite HMR serves a stale module graph after hook edits →
  `rm -rf node_modules/.vite` and restart dev server with `--force`.
- "please refactor the auth module" triggers the coding-intent heuristic
  (approval card); "update the README" does not.
- Settings hands list shows per-capability rows (duplicate-looking) — known
  cosmetic, leave it.
- `core_token` in config.py is legacy/unused; auth uses `auth_token`.
