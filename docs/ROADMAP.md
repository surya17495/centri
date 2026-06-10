# CENTRI Roadmap

Design principle across every phase: **events are the source of truth; memory is
a derived, re-derivable index.**

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

## Phase 3 — Voice

- Push-to-talk, streaming STT/TTS, barge-in.
- Reintroduce the `voice.*` event families behind the clean interface stubbed in
  Phase 0.

## Phase 4 — Productization

- Onboarding wizard, BYOK configuration UI, packaging/distribution.
- Bundled-subscription option for users who don't bring their own keys.
