# Centri — Hermes Integration

Centri is a memory-native agent built on an **OpenCode fork** plus a standalone
**Centri core memory API**. This document covers wiring the two together: running
the core as a Hermes `memory.provider`, ingesting structured Hermes chat through
the event spine, and operating the systemd services that keep both halves alive.

> See also: [`docs/centri-app.md`](centri-app.md) (OpenCode fork delta),
> [`docs/memory-architecture.md`](memory-architecture.md),
> [`docs/event-contract.md`](event-contract.md), [`docs/DEPLOY.md`](DEPLOY.md),
> and the deployable plugin under [`deploy/hermes-plugin/`](../deploy/hermes-plugin/).

---

## 1. What Centri is

Centri is two halves that share one durable memory:

| Half | What it is | Lives in |
| --- | --- | --- |
| **Centri core** | A Python memory API: an append-only event **spine**, a typed **memory graph** with bi-temporal supersession, deterministic **curation**, optional **LLM consolidation**, and a REST/WS surface. | `core/` |
| **OpenCode fork** | The TypeScript/Bun OpenCode app shell, patched (each patch marked `// CENTRI`) so every turn recalls a brief from the core and every runtime event is tapped back into the spine. All memory calls **fail open** — a dead/slow core never blocks the agent loop. | `packages/opencode/src/centri/` |

The **event spine is the source of truth**; the memory graph is a derived,
re-derivable index over it. The context window is a cache, not storage.

### Endpoints the integration uses (bridge API)

| Route | Path | Purpose |
| --- | --- | --- |
| Recall | `POST /memory/recall` | Per-turn cued brief — pure `curate()`, no LLM at read time. Returns `{ markdown, items, … }` with a `source_event_id` receipt on every item. |
| Import | `POST /events/import` | Batch-import typed event envelopes into the spine. Idempotent on `(source, payload.event_uid)`. |
| Ambient | `GET /memory/ambient.md?token=` | The consolidation-maintained standing-context layer as plain markdown (drops into a system prompt verbatim). Token rides as a query param because an `<a href>`/instruction fetcher cannot set headers. |
| Briefing | `GET /briefing` | Hands-ready briefing (identity, repo state, active session). |
| Temporal | `GET /memory/since`, `GET /memory/where-left-off` | "What changed since X" / "where did we leave off" views over the spine. |
| Health | `GET /health` | Unauthenticated liveness. |

All REST routes (except `/health`) require `Authorization: Bearer <CENTRI_AUTH_TOKEN>`.
Empty token = auth off (localhost dev only).

---

## 2. The Hermes plugin

The Hermes-facing adapter is a thin `MemoryProvider` that translates Hermes
memory calls into Centri HTTP requests. The canonical, deployable copy lives in
this repo at [`deploy/hermes-plugin/centri/`](../deploy/hermes-plugin/centri/).

### Install path

Hermes loads plugins from `~/.hermes/plugins/<name>/`. Install Centri by copying
or symlinking the repo copy:

```bash
# Symlink (recommended — stays in sync with git pulls):
mkdir -p ~/.hermes/plugins
ln -s "$PWD/deploy/hermes-plugin/centri" ~/.hermes/plugins/centri

# …or copy:
mkdir -p ~/.hermes/plugins
cp -r deploy/hermes-plugin/centri ~/.hermes/plugins/centri
```

The plugin file is `__init__.py` + `plugin.yaml`. It exposes three tools to the
agent — `centri_retain`, `centri_recall`, `centri_reflect` — and implements the
Hermes `MemoryProvider` hooks:

| Hook | What it does |
| --- | --- |
| `initialize(session_id, …)` | Records the active session/thread id. |
| `prefetch(query)` | `POST /memory/recall` → cued brief, returned as `## Centri Context`. |
| `sync_turn(user, assistant, messages)` | Batches `hermes.user.message` + `hermes.assistant.message` + `hermes.tool.result` envelopes into one `POST /events/import` (see §4). Falls back to `/utterance` if import fails. |
| `on_memory_write(action, target, content)` | Imports a `hermes.memory.write` envelope. |
| `on_session_switch(new_session_id, …)` | Repoints the active thread id. |
| `is_available()` | `GET /health`. |

### Config & auth token

The plugin reads its config from Hermes' `config.yaml` (the `memory.centri` and
`plugins.centri` blocks are merged), falling back to environment variables. Set
`memory.provider: centri` and point it at the core:

```yaml
# ~/.hermes/config.yaml  (or wherever Hermes loads config from)
memory:
  provider: centri
  centri:
    api_base: http://127.0.0.1:8760
    auth_token: <your CENTRI_AUTH_TOKEN>
```

Equivalent environment variables (checked after the config file):

```bash
export CENTRI_API_BASE=http://127.0.0.1:8760   # default if unset
export CENTRI_AUTH_TOKEN=<your CENTRI_AUTH_TOKEN>
```

`auth_token` must equal the core's `CENTRI_AUTH_TOKEN` (the same value the
OpenCode fork uses as `CENTRI_TOKEN`). Generate one with
`openssl rand -hex 32`.

> **Restart Hermes after any plugin change** — new/edited files under
> `~/.hermes/plugins/` are loaded at Hermes startup, not hot-swapped:
> `systemctl restart hermes` (or however you launch Hermes).

---

## 3. systemd services

The box runs two primary services (plus the optional Hermes dashboard):

| Service | Port | Command | Purpose |
| --- | --- | --- | --- |
| `centri-core.service` | **8760** | `centri serve` | The Centri core (FastAPI event spine + memory graph). |
| `opencode.service` | **4096** | `opencode web --hostname 0.0.0.0 --port 4096` | The OpenCode fork web server (recall + event tap wired to the core). |
| `hermes-dashboard.service` | 9119 | `hermes dashboard …` | Optional Hermes dashboard UI. |

The `opencode.service` unit points the fork at the core via:

```
Environment="CENTRI_URL=http://127.0.0.1:8760"
Environment="CENTRI_TOKEN=<CENTRI_AUTH_TOKEN>"
```

`CENTRI_URL` is the on/off switch for the whole fork integration — when unset,
the app behaves exactly like upstream OpenCode (no recall, no event import).
`CENTRI_TOKEN` maps to the core's `CENTRI_AUTH_TOKEN`.

### Install the units (in-place dev layout)

The units in `/etc/systemd/system/` reference the in-repo paths. A minimal
`centri-core.service`:

```ini
[Unit]
Description=Centri Core API Server
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/centri/core
Environment="HOME=/home/ubuntu"
EnvironmentFile=/home/ubuntu/centri/.env
ExecStart=/home/ubuntu/centri/core/.venv/bin/centri serve
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

A minimal `opencode.service`:

```ini
[Unit]
Description=Centri (OpenCode fork) Remote Server
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu
Environment="HOME=/home/ubuntu"
Environment="NODE_ENV=production"
EnvironmentFile=-/home/ubuntu/.hermes/.env
Environment="CENTRI_URL=http://127.0.0.1:8760"
Environment="CENTRI_TOKEN=<CENTRI_AUTH_TOKEN>"
ExecStart=/home/ubuntu/centri/packages/opencode/dist/opencode-linux-arm64/bin/opencode web --hostname 0.0.0.0 --port 4096
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```

For the production `/opt/centri` install path (dedicated `centri` user, Caddy
TLS), use [`deploy/install.sh`](../deploy/install.sh) and the
[`deploy/centri.service`](../deploy/centri.service) template — see
[`deploy/README.md`](../deploy/README.md).

---

## 4. Structured Hermes chat ingestion (`/events/import`)

Hermes chat is ingested as **typed, dedupable envelopes** — not flattened chat
text. The plugin's `sync_turn` builds one batch per turn and posts it to
`POST /events/import`. Each envelope follows
[`docs/event-contract.md`](event-contract.md):

```jsonc
{
  "events": [
    {
      "type": "hermes.user.message",
      "source": "hermes_turn_sync",
      "session_id": "<thread>",
      "thread_id": "<thread>",
      "payload": {
        "event_uid": "<uuid hex>",      // REQUIRED — dedupe key
        "role": "user",
        "text": "<user message, capped at 8000 chars>",
        "thread_id": "<thread>"
      }
    },
    {
      "type": "hermes.assistant.message",
      "source": "hermes_turn_sync",
      "session_id": "<thread>",
      "thread_id": "<thread>",
      "payload": { "event_uid": "<uuid>", "role": "assistant", "text": "…", "thread_id": "<thread>" }
    },
    {
      "type": "hermes.tool.result",
      "source": "hermes_turn_sync",
      "session_id": "<thread>",
      "thread_id": "<thread>",
      "payload": { "event_uid": "<uuid>", "role": "tool", "text": "…", "thread_id": "<thread>" }
    }
  ]
}
```

### The three role event types

| `type` | When it's emitted | Folded by consolidation as |
| --- | --- | --- |
| `hermes.user.message` | Each user turn. | Captured on the spine; user prompts are not auto-folded into facts (a question is not a decision). |
| `hermes.assistant.message` | Each assistant turn. | `fact` topic hint → graph fact with provenance. |
| `hermes.tool.result` | Tool calls/results in the `messages` list (role `tool`/`function`, or content shaped like tool output). | Tool-result `fact` hint → graph fact. |

(A fourth type, `hermes.memory.write`, is emitted by the plugin's
`on_memory_write` hook for explicit writes.)

### Contract guarantees

- **Idempotent.** The deterministic event id is `import:<source>:<event_uid>`.
  Re-posting a batch imports nothing new (`{accepted, duplicates, rejected}`).
  Envelopes missing `payload.event_uid` or `type` are **rejected**, never
  blind-imported.
- **Redacted before persistence.** `db.append_event` runs `redact_jsonable` on
  the payload *before* it touches the append-only ledger (a leaked secret would
  otherwise persist forever). See `core/src/centri/db.py`.
- **Folded like native events.** Imported events are picked up by the
  consolidation worker on the next scheduler tick, exactly like events the core
  itself produced.
- **CTS coverage:** `core/tests/test_hermes_integration.py` posts all four event
  types and asserts `accepted == 4` plus presence on `/events`.

---

## 5. OpenCode ingestion & the shared memory DB

There are **two ingestion paths into the same spine**, both ending up in one
SQLite database:

### The shared state DB

```
CENTRI_DB_PATH=~/.centri/state.db      # the single memory spine + graph
```

Everything — Hermes chat, OpenCode runtime events, ingested histories, the typed
graph, the FTS index, the ambient digest — lives in this one file. The core
reads `~/.centri/state.db` by default. `core/.env` is a **symlink** to the repo
root `.env`, so the core and the OpenCode fork share one configuration source.

> `.env` and `*.db` are gitignored — secrets and memory never enter the repo.

#### Path A — OpenCode runtime tap (live, structured)

`packages/opencode/src/centri/tap.ts` installs one idempotent listener on the
process-global `GlobalBus` at server boot and maps runtime events →
`centri_app.*` envelopes → batched `POST /events/import`
(`Centri.importEvents`: flush every 2s or 50 events, fire-and-forget, 5s
timeout, drops a failed batch rather than retry-looping). Covers session
lifecycle, message text, tool execution, and permissions. `thread_id` = session
id; `payload.event_uid` = the bus event id (the core dedupes on
`(source, payload.event_uid)`).

#### Path B — OpenCode history tail (ambient, structured)

`core/src/centri/ingest/opencode.py` (the v2 adapter) reads OpenCode's
**structured `event` table** (`message.part.updated.1`, `session.updated.1`, …)
and turns it into typed spine events with **deterministic consolidation hints**
(tool calls → `fact`, session activity → `decision`/`open_loop`). No LLM is
needed to re-infer meaning. Read-only (`mode=ro`); idempotent (deterministic
event id from `(type, aggregate_id, seq)`); high-water-mark resume.

- One-shot: `POST /ingest/opencode` with `{ "db_path": "~/.local/share/opencode/opencode.db" }`.
- Continuous: set `CENTRI_OPENCODE_INGEST_DB` and the scheduler tails it each
  tick.
- Bootstrap all discovered histories once: `GET /ingest/discover` then
  `POST /ingest/bootstrap` (idempotent — re-running imports nothing new).

---

## 6. FTS5 verbatim recall

On top of ranked graph recall, Centri runs a **verbatim token lift** over the
spine using SQLite FTS5:

- **Index.** `CREATE VIRTUAL TABLE IF NOT EXISTS event_fts USING fts5(text, type, source, content_rowid='rowid')`
  (`core/src/centri/db.py`). On every `append_event`, if the payload has a `text`
  or `content` field, that text is indexed into `event_fts`.
- **Query.** In the read path (`centri.curation.curate`), the cue's raw words are
  quoted into an FTS5 `MATCH` query and `search_events(query, limit=5)` returns
  `VerbatimMatch { text, type, source, event_id }` rows, attached to the brief.
- **Fail-open.** If FTS5 is unavailable or the query throws, the verbatim list is
  simply empty — recall never breaks.

This is what makes "where did we leave off?" return the *exact* prior utterance,
not a paraphrase: the cue's own tokens are matched verbatim against the indexed
spine text, then promoted into the brief with a receipt.

---

## 7. GLM-5.2 consolidation

The deterministic consolidation path (hints → graph ops, no LLM) is the default
and is never bypassed. On top of it, an **optional LLM consolidation tier**
proposes typed ops for events that carry *no* deterministic hint (raw stdout,
transcripts). GLM-5.2 is the consolidation model.

- **OpenCode fork side.** `packages/opencode/src/provider/provider.ts` treats
  `glm`-family models as reasoning models with `interleaved: { field:
  "reasoning_content" }` (so reasoning streams correctly), and
  `provider/transform.ts` no longer forces a `glm` variant block.
- **Core side.** The OpenAI-compatible consolidation client
  (`core/src/centri/consolidation_llm.py`, `resolve_consolidation_client`)
  builds from settings and is **honest-unavailable**: with no
  `CENTRI_CONSOLIDATION_BASE_URL` + `CENTRI_CONSOLIDATION_MODEL` it returns
  `None` and the tier does nothing. The proposal contract
  (`consolidation_prompt.py`) emits typed ops — `add_fact`, `open_loop`,
  `decision`, `supersede`, `profile_update`, `finish` — and a **deterministic
  gatekeeper** validates → applies/rejects each one with a provenance receipt
  (`consolidation.proposal.applied` / `.rejected`). Hinted events never reach
  the tier.

```bash
# .env (commented in .env.example — uncomment + fill to enable)
CENTRI_CONSOLIDATION_BASE_URL=<openai-compatible endpoint>
CENTRI_CONSOLIDATION_MODEL=zai-org/GLM-5.2
CENTRI_CONSOLIDATION_API_KEY=<key injected at runtime — never committed>
CENTRI_CONSOLIDATION_BATCH_SIZE=16
```

Covered offline by `core/tests/test_llm_consolidation.py` (scripted fake LLM,
asserts apply/reject paths, supersession, provenance, and the
hinted-events-skipped guarantee).

---

## 8. User profile and briefing

- **User profile.** A small `mem_profile` key/value table in the graph
  (`memory_graph.py`), each entry carrying a `source_event_id` for provenance.
  The LLM consolidation tier may propose `profile_update` ops; the deterministic
  gatekeeper applies them with a receipt. The profile renders into the ambient
  standing-context layer.
- **Briefing.** `BriefingBuilder` turns a `ContextPacket` into a hands-ready
  prompt (identity, desktop/repo context, active session). The ambient layer,
  profile, active projects, top open loops, and narrative are refreshed by
  consolidation and read straight from the graph.
- **Session-start push.** `CENTRI_SESSION_BRIEF=1` (on by default) builds the
  deterministic, LLM-free "what changed / what's blocked / what's next" brief on
  session start and surfaces it unprompted (a `brief.session_start` spine event
  + first-turn context). Cheap — no model call.

Relevant endpoints: `GET /briefing`, `GET /memory/ambient.md`,
`GET /memory/since`, `GET /memory/where-left-off`.

---

## 9. Install / start / verify

### One-time install (core)

```bash
git clone https://github.com/surya17495/centri.git
cd centri
cp .env.example .env                # fill in BYOK keys + set CENTRI_AUTH_TOKEN
# core/.env symlinks to ./ .env — the core reads it automatically
cd core
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"

# set the auth token (used by the core, the fork, and the Hermes plugin)
echo "CENTRI_AUTH_TOKEN=$(openssl rand -hex 32)"   # put this value in .env
```

### One-time install (OpenCode fork)

```bash
# from repo root
bun install
bun run packages/opencode/src/index.ts build   # produces dist/opencode-linux-arm64/bin/opencode
```

### Start the services

```bash
# core (foreground, during dev):
cd core && . .venv/bin/activate && centri serve

# …or via systemd (production):
sudo systemctl enable --now centri-core
sudo systemctl enable --now opencode
```

### Install the Hermes plugin

```bash
cd /home/ubuntu/centri
mkdir -p ~/.hermes/plugins
ln -s "$PWD/deploy/hermes-plugin/centri" ~/.hermes/plugins/centri
# add the memory.provider: centri block to ~/.hermes/config.yaml (§2)
systemctl restart hermes     # or however Hermes is launched
```

### Verify

```bash
# 1. core health (unauthenticated)
curl -fsS http://127.0.0.1:8760/health
# -> {"status":"ok","version":"0.1.0"}

# 2. auth-protected status
curl -fsS http://127.0.0.1:8760/status -H "Authorization: Bearer $CENTRI_AUTH_TOKEN"

# 3. recall (cued brief, no LLM at read time)
curl -fsS -X POST http://127.0.0.1:8760/memory/recall \
  -H "Authorization: Bearer $CENTRI_AUTH_TOKEN" \
  -H 'content-type: application/json' \
  -d '{"cue":"where did we leave off","format":"markdown+items"}'

# 4. structured import (idempotent — re-post → duplicates)
curl -fsS -X POST http://127.0.0.1:8760/events/import \
  -H "Authorization: Bearer $CENTRI_AUTH_TOKEN" \
  -H 'content-type: application/json' \
  -d '{"events":[{"type":"hermes.user.message","source":"hermes_turn_sync","thread_id":"t1","payload":{"event_uid":"u1","role":"user","text":"hello","thread_id":"t1"}}]}'
# -> {"accepted":1,"duplicates":0,"rejected":0}

# 5. OpenCode fork web UI
curl -fsS http://127.0.0.1:4096/   ;# or open http://<host>:4096 in a browser

# 6. service status
systemctl is-active centri-core opencode
ss -ltnp | grep -E ':8760|:4096'
```

---

## 10. Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| **`Executable not found in $PATH: "xdg-open"` (ENOENT) in `opencode.service` logs** | **Harmless.** OpenCode tries to auto-open a browser on startup; on a headless VM there is no `xdg-open`. The spawn fails and is logged, but the web server keeps running on `:4096`. To silence it, set an envvar to disable browser-open or install `xdg-utils` (`apt-get install -y xdg-utils`) — purely cosmetic. |
| **Core "spine not booted" / empty briefs** | The core could not read `~/.centri/state.db`. Check `CENTRI_DB_PATH` and that the path is writable. `core/.env` is a symlink to the repo-root `.env` — if the symlink is broken, the core reads defaults (often fine) or no env at all. Verify with `readlink -f core/.env` and `systemctl cat centri-core \| grep EnvironmentFile`. |
| **401 Unauthorized on `/memory/recall` / `/events/import`** | Bearer token mismatch. `CENTRI_AUTH_TOKEN` (core), `CENTRI_TOKEN` (fork unit), and `memory.centri.auth_token` (Hermes plugin) must all be the **same value**. Re-check the symlinked `.env` and the `opencode.service` `Environment=` line. |
| **Plugin / config changes not picked up by Hermes** | Hermes loads `~/.hermes/plugins/` at startup. **Restart Hermes after any plugin change** (`systemctl restart hermes`). The plugin hot-reloads *config* on each call (`_load_config()` runs per call), but new/edited plugin files need a restart. |
| **Service won't start after a `.env` edit** | `sudo systemctl restart centri-core` (and `opencode` if you changed its unit). systemd reads `EnvironmentFile=` at start; edits need a restart. Confirm with `systemctl status centri-core` and `journalctl -u centri-core -n 50`. |
| **Docker** | The `docker compose` path is **optional** and not used on this box (`docker compose` is not running here). The systemd layout above is the active one. Docker is fine for a container-only deploy — see [`docs/DEPLOY.md`](DEPLOY.md) (core on `:8760`, shell on `:8761`). |
| **Port already in use** | `ss -ltnp | grep -E ':8760|:4096'`. Only one `centri-core` / `opencode` should bind each. |
| **Recall returns empty but events were imported** | Consolidation runs on the scheduler tick, not at import time — wait a few seconds, or run `centri memory rebuild` to re-derive the graph from the spine immediately. Also check the FTS5 index exists (`event_fts` table). |

### Restart cheat-sheet

```bash
sudo systemctl restart centri-core          # after .env / code edits to the core
sudo systemctl restart opencode             # after fork rebuilds or CENTRI_* edits
sudo systemctl restart hermes               # after plugin changes
sudo systemctl restart centri-core opencode # both halves at once
journalctl -u centri-core -f                # follow core logs
journalctl -u opencode -f                   # follow fork logs
```
