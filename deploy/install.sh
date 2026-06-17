#!/usr/bin/env bash
# CENTRI Phase 3a — VM install script (Ubuntu 22.04/24.04).
#
# Usage:
#   sudo bash deploy/install.sh                  # localhost only, no TLS
#   sudo bash deploy/install.sh centri.mydomain.com   # + Caddy TLS proxy
#
# What it does (idempotent — safe to re-run):
#   1. System deps + dedicated `centri` user
#   2. Clones/updates the repo into /opt/centri and installs core into a venv
#   3. Generates CENTRI_AUTH_TOKEN (kept across re-runs) in /etc/centri/centri.env
#   4. Installs + starts the systemd service (core bound to 127.0.0.1)
#   5. If a domain is given: installs Caddy and configures auto-TLS reverse proxy
#
# After install, thin clients (any browser, or the OpenCode fork web app) connect
# to https://<domain> with the printed auth token (Settings → Backend → Auth
# token). Tauri desktop and mobile PWA are roadmap, not shipped clients.

set -euo pipefail

DOMAIN="${1:-}"
REPO_URL="${CENTRI_REPO_URL:-https://github.com/surya17495/centri.git}"
APP_DIR=/opt/centri
ENV_DIR=/etc/centri
ENV_FILE="$ENV_DIR/centri.env"
STATE_DIR=/var/lib/centri
PORT="${CENTRI_CORE_PORT:-8760}"

if [[ $EUID -ne 0 ]]; then
  echo "Run as root: sudo bash deploy/install.sh [domain]" >&2
  exit 1
fi

echo "==> [1/5] System packages"
apt-get update -q
apt-get install -y -q python3 python3-venv python3-pip git curl >/dev/null

id -u centri &>/dev/null || useradd --system --home "$STATE_DIR" --shell /usr/sbin/nologin centri

echo "==> [2/5] Code + virtualenv ($APP_DIR)"
if [[ -d "$APP_DIR/.git" ]]; then
  git -C "$APP_DIR" pull --ff-only
else
  git clone "$REPO_URL" "$APP_DIR"
fi
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -e "$APP_DIR/core"

mkdir -p "$STATE_DIR"
chown -R centri:centri "$STATE_DIR" "$APP_DIR"

echo "==> [3/5] Environment ($ENV_FILE)"
mkdir -p "$ENV_DIR"
if [[ ! -f "$ENV_FILE" ]]; then
  TOKEN="$(openssl rand -hex 32 2>/dev/null || head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  ORIGINS="http://localhost:1420,http://127.0.0.1:1420,tauri://localhost,https://tauri.localhost"
  [[ -n "$DOMAIN" ]] && ORIGINS="$ORIGINS,https://$DOMAIN"
  cat >"$ENV_FILE" <<EOF
# CENTRI core configuration — edit and \`systemctl restart centri\` to apply.
CENTRI_CORE_HOST=127.0.0.1
CENTRI_CORE_PORT=$PORT
CENTRI_AUTH_TOKEN=$TOKEN
CENTRI_DB_PATH=$STATE_DIR/state.db
CENTRI_CORS_ORIGINS=$ORIGINS
CENTRI_AUTONOMY_LEVEL=supervised
# BYOK — set your model gateway before first real use:
# LITELLM_BASE_URL=
# LITELLM_API_KEY=
EOF
  chmod 600 "$ENV_FILE"
  echo "    generated new auth token"
else
  echo "    keeping existing $ENV_FILE (token preserved)"
fi

echo "==> [4/5] systemd service"
install -m 644 "$APP_DIR/deploy/centri.service" /etc/systemd/system/centri.service
systemctl daemon-reload
systemctl enable --now centri
sleep 2
systemctl --no-pager --quiet is-active centri || {
  echo "centri failed to start; check: journalctl -u centri -n 50" >&2
  exit 1
}

if [[ -n "$DOMAIN" ]]; then
  echo "==> [5/5] Caddy TLS proxy for $DOMAIN"
  if ! command -v caddy >/dev/null; then
    apt-get install -y -q debian-keyring debian-archive-keyring apt-transport-https >/dev/null
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' |
      gpg --yes --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' |
      tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
    apt-get update -q && apt-get install -y -q caddy >/dev/null
  fi
  sed "s/centri.example.com/$DOMAIN/; s/127.0.0.1:8760/127.0.0.1:$PORT/" \
    "$APP_DIR/deploy/Caddyfile" >/etc/caddy/Caddyfile
  systemctl reload caddy || systemctl restart caddy
  URL="https://$DOMAIN"
else
  echo "==> [5/5] No domain given — skipping TLS proxy (core on 127.0.0.1:$PORT)"
  URL="http://127.0.0.1:$PORT"
fi

TOKEN_LINE="$(grep ^CENTRI_AUTH_TOKEN "$ENV_FILE")"
echo
echo "------------------------------------------------------------"
echo " CENTRI core is running."
echo "   URL:        $URL"
echo "   Health:     curl $URL/health"
echo "   Auth:       ${TOKEN_LINE}"
echo "   Clients:    shell Settings → Backend URL + Auth token"
echo "   Logs:       journalctl -u centri -f"
echo "------------------------------------------------------------"
