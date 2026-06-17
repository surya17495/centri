# CENTRI — VM deployment (Phase 3a)

Run the CENTRI core on a small VM; connect thin clients (any browser, or the
OpenCode fork web app) over TLS with a shared auth token. A Tauri desktop binary
and mobile PWA are scaffolded/roadmap, not shipped built clients.

## Quick start

```bash
# On a fresh Ubuntu 22.04/24.04 VM with a DNS A record pointing at it:
git clone https://github.com/surya17495/centri.git
sudo bash centri/deploy/install.sh centri.yourdomain.com
```

The script prints the generated `CENTRI_AUTH_TOKEN`. In each client open
Settings → Backend, set the URL to `https://centri.yourdomain.com` and paste
the token. Without a domain (`sudo bash deploy/install.sh`) the core stays on
`http://127.0.0.1:8760` — use an SSH tunnel from your laptop:
`ssh -L 8760:127.0.0.1:8760 user@vm`.

## First-run memory bootstrap (3b.4)

A fresh install starts with empty memory. To seed it with the coding-agent
histories already on the machine (OpenCode, Claude Code, Cursor), run a one-time
**bootstrap** after the core is up. Because ingestion is high-water-mark based,
bootstrap is simply the *first tick* — re-running it imports nothing new, and the
scheduler's ambient tail continues from where bootstrap left off.

```bash
# 1. Ask what's discoverable first (read-only — counts, no import):
curl -s http://127.0.0.1:8760/ingest/discover \
  -H "Authorization: Bearer $CENTRI_AUTH_TOKEN" | jq
# → {"sources":[{"agent":"opencode","count":1400,...},
#                {"agent":"claude_code","count":600,...}], "total_messages":2000, ...}

# 2. Import everything discovered (emits ingest.bootstrap.* events on the timeline):
curl -s -X POST http://127.0.0.1:8760/ingest/bootstrap \
  -H "Authorization: Bearer $CENTRI_AUTH_TOKEN" \
  -H 'Content-Type: application/json' -d '{}' | jq
# → {"imported":2000,"source_count":2,...}
```

Discovery probes well-known per-platform defaults (`~/.local/share/opencode`,
`~/.claude/projects`, Cursor `state.vscdb` under `~/.config/Cursor` /
`~/Library/Application Support/Cursor`). Override or extend per agent with
`CENTRI_INGEST_OPENCODE_PATHS` / `CENTRI_INGEST_CLAUDE_CODE_PATHS` /
`CENTRI_INGEST_CURSOR_PATHS` (comma-separated), or opt an agent out with
`CENTRI_INGEST_DISABLED_AGENTS`. On a headless VM the agents' stores usually live
on the developer's laptop, not the VM — run bootstrap where the histories are, or
point the override env vars at a synced copy.

## Security model

| Layer | Mechanism |
| --- | --- |
| Transport | Caddy terminates TLS (auto Let's Encrypt issue + renew) |
| AuthN | Shared-secret bearer token (`CENTRI_AUTH_TOKEN`), constant-time compare |
| REST | `Authorization: Bearer <token>` on every route except `/health` |
| WebSocket | `wss://…/events/stream?token=<token>` (browsers cannot set WS headers) |
| Exposure | Core binds `127.0.0.1` only; Caddy is the sole public listener |
| Service | systemd unit runs as the unprivileged `centri` user, `NoNewPrivileges`, state confined to `/var/lib/centri` |

Empty `CENTRI_AUTH_TOKEN` disables auth — acceptable only for localhost dev,
which is why `install.sh` always generates one.

## Files

- `install.sh` — idempotent provisioner (deps, venv, env file, systemd, Caddy)
- `centri.service` — systemd unit template
- `Caddyfile` — TLS reverse-proxy template

## Operations

```bash
journalctl -u centri -f            # logs
systemctl restart centri           # apply /etc/centri/centri.env edits
sudo bash deploy/install.sh <dom>  # re-run = git pull + reinstall (token kept)
```

## Verification status (honest)

Sandbox-verified (this repo's CI-equivalent):
- Bearer/WS auth enforcement: `core/tests/test_centri.py::TestAuth` (5 tests)
- Live check: uvicorn + `CENTRI_AUTH_TOKEN` → 401 without / 200 with token,
  WS handshake rejected without `?token=`
- `bash -n` syntax check of `install.sh`

- Memory bootstrap (3b.4): adapter registry, discovery, and import are
  fixture-verified (`core/tests/test_ingest_*.py`, `test_centri.py::TestBootstrap`)

Needs a real VM + domain (not possible in the dev sandbox):
- systemd unit lifecycle, Caddy install, Let's Encrypt issuance, end-to-end
  `https://`/`wss://` from a remote client
- Bootstrap against **real** OpenCode/Claude Code/Cursor on-disk stores — their
  schemas vary across releases; the readers are built tolerant but proven only
  against fixtures so far.
