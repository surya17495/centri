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

## Phase 1 — Coding loop (text-first) + Tauri shell

- Tauri 2 + React desktop shell over HTTP + `/events/stream`.
- Implement the ACP wire protocol (JSON-RPC over stdio) so `AcpHand` runs real
  external agents alongside OpenCode.
- End-to-end text-first coding loop with live progress, artifacts, and approvals.

## Phase 2 — Memory v1 + briefing

Design: [`memory-architecture.md`](memory-architecture.md). Benchmark:
[`centri-bench.md`](centri-bench.md).

- Memory synthesis worker ("sleep cycle"): fold event batches into typed
  decision/fact/open-loop objects and core blocks continuously — typed objects
  with receipts, never freeform prose; conflicts resolved by supersession.
- Cue-driven injection: assemble relevant decisions, rejections, conventions, and
  open alternatives into the hand brief at delegation/session-start/repo-open.
- Proactive briefing ("what changed, what's blocked, what's next") plus dormancy
  detection (one yes/no line per dormant loop, the only allowed spoonfeeding).
- Prove re-derivability at scale via `rebuild_from_events()`.
- Escape-hatch validation: `LettaMemoryStore` adapter run head-to-head against
  CENTRI native in `centri-bench`.

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
