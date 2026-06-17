# CENTRI

**A memory-first, stateful agent with one core and a photographic memory of everything
you and your coding agents have done.** CENTRI remembers; it delegates the real work to
coding agents (OpenCode over ACP) and tools (Composio), and folds every result back into
a single, durable memory.

## Why

Agents forget. Context windows fill, sessions die, and every new chat starts cold — so
you re-explain decisions you already made and watch the agent re-propose approaches you
already rejected. CENTRI inverts this: an append-only **event ledger** is the source of
truth, **memory is a derived index** that can be thrown away and re-derived from the
ledger, and every per-turn context is **assembled fresh** by a deterministic curation
function that attaches a receipt to every line. The context window is a cache, not
storage.

## Key capabilities

- **Typed memory graph with bi-temporal supersession** — decisions, facts, and open
  loops. New truth invalidates old truth, but history is retained: stale facts never
  resurface in a brief, while the full timeline stays auditable.
- **Deterministic curation with receipts** — each per-turn brief is rendered by one
  `curate()` path, and every line carries a `source_event_id` pointing back to the
  ledger event it came from. The same `(graph, cue, budget, policy)` yields a
  byte-identical brief.
- **Imports existing histories** — one-shot bootstrap plus a continuous tail of your
  OpenCode, Claude Code, and Cursor histories, so a fresh install starts with complete
  memory instead of a blank slate.
- **Temporal recall** — ask "what changed since yesterday" or "where did we leave off"
  and get an answer grounded in the ledger's timeline, not a guess.
- **Coding delegation over ACP** — work is handed to a coding agent (Agent Client
  Protocol, JSON-RPC over stdio) with live streaming progress, an approval gate for
  destructive actions, and automatic failover to a fallback hand when the primary peer
  is unreachable.
- **First-class tool contract** — tools (Composio, e.g. Tavily search) sit beside the
  coding hands. Every invocation is event-ledgered, side-effectful tools pass through
  the approval gate, read-only results are folded back into the memory graph.
- **Multi-client** — any shell (desktop or web) talks to the same core over a
  bearer-authenticated API; nothing client-side is authoritative.
- **Benchmarked** — composite **1.00** vs a **real Letta server** at **0.93**, the gap
  entirely on stale-fact supersession. See [`docs/centri-bench.md`](docs/centri-bench.md).

## Architecture

Events are the source of truth; memory is a derived, re-derivable index over them.

| Layer        | What it is                                                                       |
|--------------|----------------------------------------------------------------------------------|
| Shell        | Tauri 2 + React desktop/web surface — activity timeline, task cards, approvals   |
| Coordinator  | Python core loop: understand → decide → act → narrate → remember                 |
| Event spine  | Append-only SQLite ledger + in-memory bus, with secret redaction on write        |
| Memory       | Typed decisions/facts/open-loops with bi-temporal supersession, derived from the spine |
| Hands        | Capability router over coding agents — ACP preferred, native OpenCode fallback   |
| Tools        | `ToolProvider` contract with Composio; event-ledgered, approval-gated invocation |

See [`docs/architecture.md`](docs/architecture.md),
[`docs/memory-architecture.md`](docs/memory-architecture.md),
[`docs/event-contract.md`](docs/event-contract.md), and
[`docs/ROADMAP.md`](docs/ROADMAP.md) for detail.

## Quickstart

### Docker

```bash
cp .env.example .env        # fill in your model gateway keys (BYOK)
docker compose up -d        # core on :8760, web shell on :8761
```

### Manual

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

## Configuration

All configuration is environment-driven — copy [`.env.example`](.env.example) to `.env`
and fill in your keys. Nothing is committed; `.env` and `*.db` are gitignored.

CENTRI is BYOK (bring your own keys) and model-agnostic:

- **Model gateway** — `LITELLM_BASE_URL` / `LITELLM_API_KEY` point at any
  OpenAI-compatible provider (e.g. Pioneer or Nebius Token Factory). Role models are set
  per task (`MODEL_INTENT`, `MODEL_REASONING`, …).
- **Auth** — `CENTRI_AUTH_TOKEN` gates every REST route except `/health` (and the
  `/events/stream` WebSocket via `?token=`). Empty means auth off; set it before
  exposing a port.
- **Tools** — `CENTRI_COMPOSIO_API_KEY` enables Composio; `CENTRI_COMPOSIO_TOOLS` is a
  comma-separated allowlist of tool slugs (default `TAVILY_SEARCH`). With no key the
  provider reports unavailable-with-reason and never touches the network.
- **Embeddings (optional)** — off by default (lexical recall only). Turn on semantic
  recall with `CENTRI_EMBEDDING_*`; for the network route via Nebius Token Factory:

  ```bash
  CENTRI_EMBEDDING_ENABLED=true
  CENTRI_EMBEDDING_MODEL=openai/Qwen/Qwen3-Embedding-8B
  LITELLM_BASE_URL=https://api.tokenfactory.nebius.com/v1/
  LITELLM_API_KEY=your-nebius-token-factory-key
  ```

- **Ingest paths** — `CENTRI_INGEST_OPENCODE_PATHS`, `CENTRI_INGEST_CLAUDE_CODE_PATHS`,
  and `CENTRI_INGEST_CURSOR_PATHS` override the per-platform probe for histories in
  unusual locations; `CENTRI_INGEST_DISABLED_AGENTS` opts an agent out entirely.

## Hermes integration

Centri also ships as a **Hermes `memory.provider`**. A thin plugin
(`CentriMemoryProvider`) translates Hermes memory calls into the core's HTTP
API: `prefetch` → `POST /memory/recall`, `sync_turn` / `on_memory_write` →
batched `POST /events/import`. The deployable plugin lives at
[`deploy/hermes-plugin/centri/`](deploy/hermes-plugin/centri/).

```yaml
# ~/.hermes/config.yaml
memory:
  provider: centri
  centri:
    api_base: http://127.0.0.1:8760
    auth_token: <CENTRI_AUTH_TOKEN>   # same value as the core + the fork's CENTRI_TOKEN
```

Structured Hermes chat is ingested as typed, dedupable envelopes
(`hermes.user.message`, `hermes.assistant.message`, `hermes.tool.result`,
`hermes.memory.write`) — not flattened text. Restart Hermes after any plugin
change. Full guide: [`docs/HERMES-INTEGRATION.md`](docs/HERMES-INTEGRATION.md).

### Running it as systemd services

The reference VM runs two services sharing one memory DB
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

The `xdg-open` ENOENT line in `opencode.service` logs is harmless — OpenCode
tries to auto-open a browser on a headless box and keeps running after the
_spawn_ fails. See [troubleshooting](docs/HERMES-INTEGRATION.md#10-troubleshooting).

## Memory import

A fresh install imports your existing coding-agent histories so memory is complete from
day one. Discover what's available, then bootstrap once (idempotent — re-running imports
nothing new):

```bash
curl localhost:8760/ingest/discover            # "found N OpenCode messages, M Cursor sessions"
curl -X POST localhost:8760/ingest/bootstrap \
  -H 'content-type: application/json' -d '{}'   # one-time full import
```

After bootstrap, the ambient tail keeps pulling new history each scheduler tick.

## Benchmark

`centri-bench` is a falsifiable head-to-head: each engine assembles a per-turn brief
from the same persona ground truth, scored on brief completeness, re-proposal rate,
next-step correctness, and stale-fact handling. Native CENTRI is run against a **real
Letta server** (v0.16.8, pgvector archival) — both a deterministic rubric and an
LLM judge agree on the result.

| Metric (avg over 3 personas) | native | Letta |
|------------------------------|:------:|:-----:|
| brief completeness ↑         | 1.00   | 1.00  |
| re-proposal rate ↓           | 0.00   | 0.00  |
| next-step correct ↑          | 1.00   | 1.00  |
| stale-fact correct ↑         | 1.00   | 0.67  |
| **composite ↑**              | **1.00** | **0.93** |

The whole gap is stale-fact supersession: on a rename, Letta's semantic retrieval
returns both the stale and the current note, while CENTRI's typed graph supersedes the
old one. Letta is a **benchmark comparison only, not a runtime dependency** — CENTRI
runs without it. Run it yourself:

```bash
cd core
python -m centri.bench.run            # human-readable report
python -m centri.bench.run --json     # machine-readable scores
```

See [`docs/centri-bench.md`](docs/centri-bench.md) for the full methodology.

## Status

The core, memory graph, coding hands, and tool contract are verified by a 365-test
suite (`cd core && python -m pytest tests/`), covering the event spine with
redaction, the real ACP coding loop against the `opencode` binary with error-path and
failover drills, the typed memory graph with supersession, deterministic curation
parity between chat and coding turns, bearer auth, and the Composio tool path. The
React web shell builds, typechecks, and runs in a browser. The Tauri desktop wrapper is
scaffolded but needs a local Rust toolchain to build. Voice is next on the roadmap — see
[`docs/ROADMAP.md`](docs/ROADMAP.md).

## License

CENTRI is licensed under **FSL-1.1-Apache-2.0** (Functional Source License 1.1
with an Apache-2.0 future grant) — see [LICENSE](LICENSE). You are free to use,
modify, and self-host it for any purpose other than a competing commercial use.
The competing-use restriction lapses two years after each release, at which
point that release converts to Apache-2.0.

Runtime dependencies are permissively licensed (MIT/BSD/Apache-2.0). OpenCode is
an external MIT-licensed process that CENTRI speaks to over ACP, not a bundled
or derivative component. `letta-client` is bench-only tooling, not a product
dependency.
