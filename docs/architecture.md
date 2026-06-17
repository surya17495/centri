# CENTRI Architecture

**Events are the source of truth; memory is a derived, re-derivable index.**

CENTRI is a Python core that turns a user's intent into delegated coding work,
keeps durable state on an append-only event spine, and derives memory from it.

## Component diagram

```
                    ┌──────────────────────────────┐
                    │   Web UI  (OpenCode fork /    │
                    │   React shell) — text surface │
                    └───────────────┬──────────────┘
                                    │ HTTP + WebSocket
                    ┌───────────────▼──────────────┐
                    │         FastAPI app           │  /utterance /status
                    │      (centri.app + runtime)   │  /events/stream …
                    └───────────────┬──────────────┘
                                    │
                    ┌───────────────▼──────────────┐
                    │         Coordinator           │  understand → decide →
                    │  intent → permissions → hand  │  act → narrate → remember
                    └───┬──────────┬──────────┬─────┘
                        │          │          │
          ┌─────────────▼──┐  ┌────▼─────┐  ┌─▼──────────────┐
          │  Event spine   │  │  Memory  │  │     Hands      │
          │  SQLite ledger │  │ (derived │  │  router        │
          │  + redaction   │  │  index)  │  │                │
          │  + event bus   │  │          │  │ ┌────────────┐ │
          └────────────────┘  └────┬─────┘  │ │ OpenCode   │ │ subprocess
                  ▲                 │        │ ├────────────┤ │
                  │ rebuild_from_   │        │ │ ACP (stub) │ │ JSON-RPC/stdio
                  │ events()        │        │ └────────────┘ │
                  └─────────────────┘        └────────────────┘
```

## Components

- **Shell** — the OpenCode fork web app (and an optional React shell in
  `shell/`). Talks to the core over HTTP and the `/events/stream` WebSocket. The
  Tauri desktop wrapper (`shell/src-tauri/`) is scaffolded but not a shipped
  built binary; use the web app.
- **Coordinator** (`coordinator.py`) — the brain. Classifies intent, checks
  permissions, assembles hot context, hands off to a capability, narrates, and
  records events. Hot path reads from the context cache (<50 ms); DB and memory
  enrichment happen in the background.
- **Event spine** (`db.py`, `event_bus.py`, `redaction.py`) — append-only SQLite
  ledger plus an in-memory fan-out bus. Every event is **redacted before
  persistence**. This is the system's source of truth.
- **Memory** (`memory.py`, `memory_store.py`) — a *derived* index over the spine.
  `MemoryStore` exposes core blocks (`active_project`, `open_loops`,
  `priorities`, `people`), archival facts, a synthesis hook, and
  `rebuild_from_events()`. SQLite is the always-available native projection; a
  `LettaMemoryStore` adapter exists for benchmark comparison only and is not a
  runtime requirement.
- **Hands** (`hands/`) — capability router over the `Hand` ABC. The coordinator
  hands off by capability name; the router picks a configured hand. `OpenCodeHand`
  drives the `opencode` CLI as a subprocess; `AcpHand` is a real Agent Client
  Protocol client (JSON-RPC over stdio) that launches the agent, streams progress,
  and routes destructive actions through the approval gate.
- **Jobs / Scheduler** (`jobs.py`, `scheduler.py`) — run handoffs to completion,
  persist progress/artifact/completion events, recover in-flight work on boot.
- **Model router** (`model_router.py`) — BYOK role-based model resolution through
  LiteLLM or a direct provider (Nebius/OpenAI).

## Honest-failure principle

A capability is **configured and healthy**, or **unavailable with a reason**.
There is no placeholder "connected" state. `health()` on a hand returns
`HandHealth(healthy, reason)`; `/status` surfaces each capability's real health;
unconfigured providers report unavailable rather than faking success.
