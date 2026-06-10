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

- Memory synthesis worker: fold event batches into core blocks and archival facts
  continuously.
- Proactive briefing ("what changed, what's blocked, what's next").
- Prove re-derivability at scale via `rebuild_from_events()`.

## Phase 3 — Voice

- Push-to-talk, streaming STT/TTS, barge-in.
- Reintroduce the `voice.*` event families behind the clean interface stubbed in
  Phase 0.

## Phase 4 — Productization

- Onboarding wizard, BYOK configuration UI, packaging/distribution.
- Bundled-subscription option for users who don't bring their own keys.
