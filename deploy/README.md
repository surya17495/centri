# CENTRI — VM deployment (Phase 3a)

Run the CENTRI core on a small VM; connect thin clients (Tauri shell on
Windows/Mac, any browser, mobile PWA) over TLS with a shared auth token.

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

Needs a real VM + domain (not possible in the dev sandbox):
- systemd unit lifecycle, Caddy install, Let's Encrypt issuance, end-to-end
  `https://`/`wss://` from a remote client
