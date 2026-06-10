# CENTRI

CENTRI is a voice-first, stateful, ambient builder agent for technical founders.
You talk to it about what you're building; it delegates the actual coding to
external coding agents and keeps durable, auditable track of everything that
happens. It is model-agnostic — bring your own LLM stack (BYOK) or, later, use a
bundled subscription.

Coding work is delegated through a **hand** abstraction. The default bundled hand
is OpenCode (driven as a CLI subprocess); the Agent Client Protocol (ACP, JSON-RPC
over stdio) hand lands in Phase 1 so any ACP-compatible agent can be plugged in.

The core design principle is that **events are the source of truth; memory is a
derived, re-derivable index.** Every runtime event is written to an append-only
SQLite ledger (after secret redaction), and CENTRI's memory — core context blocks
and archival facts — is a projection that can be thrown away and rebuilt by
replaying that ledger. Nothing in memory is authoritative; the spine is.

## Architecture

| Layer        | Phase 0 status            | What it is                                                        |
|--------------|---------------------------|-------------------------------------------------------------------|
| Shell        | Phase 1 (not yet)         | Tauri 2 + React desktop surface (voice/text)                      |
| Coordinator  | working                   | Python core: understand → decide → act → narrate → remember       |
| Event spine  | working                   | SQLite append-only ledger + in-memory bus, **redaction on write** |
| Memory       | skeleton (`MemoryStore`)  | Derived, re-derivable index over the spine; Letta optional        |
| Hands        | OpenCode working, ACP stub| Capability router over the `Hand` ABC (OpenCode + ACP)            |

See [`docs/architecture.md`](docs/architecture.md),
[`docs/event-contract.md`](docs/event-contract.md), and
[`docs/ROADMAP.md`](docs/ROADMAP.md) for detail.

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

- **Working:** event spine with redaction-before-persistence; FastAPI app with
  `/health`, `/status`, `/utterance`, tasks/approvals/threads/events,
  `/events/stream` WebSocket; coordinator intent → handoff → job loop; OpenCode
  hand (when the `opencode` CLI is on PATH); SQLite memory store with
  `rebuild_from_events()`; BYOK model router.
- **Honest-unavailable:** the ACP hand (Phase 1 wire protocol); voice endpoints
  (Phase 3); Letta semantic memory unless `CENTRI_LETTA_URL` is configured. These
  report unavailable-with-reason rather than faking success.
- **Not here yet:** the Tauri shell, voice, memory synthesis worker, subscriptions.

## Configuration

All configuration is environment-driven (see `.env.example`). No secrets are
committed; `.env` and `*.db` are gitignored.
