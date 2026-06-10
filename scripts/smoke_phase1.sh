#!/usr/bin/env bash
#
# Phase 1 end-to-end smoke test.
#
# Proves the text-first coding loop works through the real seams: a user
# utterance creates a task, the ACP hand (pointed at the scripted fake ACP
# agent) streams progress events live over the WebSocket, the agent's
# permission request surfaces as approval.requested, and approving it over
# REST lets the turn complete (task.completed).
#
# Everything runs against a real uvicorn server, a real WebSocket, and a real
# ACP subprocess over stdio — nothing is mocked.
#
# Usage:  scripts/smoke_phase1.sh
# Exit 0 on success; non-zero with a diagnostic on any failed assertion.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CORE_DIR="$REPO_ROOT/core"
FAKE_AGENT="$CORE_DIR/tests/acp_fake_agent.py"

PORT="${CENTRI_SMOKE_PORT:-8799}"
HOST="127.0.0.1"
BASE="http://$HOST:$PORT"

PYTHON_BIN="${PYTHON_BIN:-}"
if [[ -z "$PYTHON_BIN" ]]; then
  if [[ -x "$CORE_DIR/.venv/bin/python" ]]; then
    PYTHON_BIN="$CORE_DIR/.venv/bin/python"
  else
    PYTHON_BIN="$(command -v python3 || command -v python)"
  fi
fi
# Resolve to an absolute path: the server is launched after a `cd` into core/,
# so a relative interpreter path would break.
PYTHON_BIN="$(cd "$(dirname "$PYTHON_BIN")" && pwd)/$(basename "$PYTHON_BIN")"

WORKDIR="$(mktemp -d)"
SERVER_LOG="$WORKDIR/server.log"
SERVER_PID=""

cleanup() {
  if [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  rm -rf "$WORKDIR"
}
trap cleanup EXIT

echo "==> Booting CENTRI backend on $BASE (ACP hand -> fake agent, permission mode)"

# Point the ACP hand at the scripted fake agent and make ACP the only/primary
# hand so the coding task is guaranteed to route through it.
export CENTRI_ACP_COMMAND="$PYTHON_BIN $FAKE_AGENT"
export CENTRI_ENABLED_HANDS="acp"
export CENTRI_HAND_PRIORITY="acp"
export CENTRI_AUTONOMY_LEVEL="autonomous_local"
export CENTRI_CORE_PORT="$PORT"
export CENTRI_CORE_HOST="$HOST"
export ACP_FAKE_MODE="permission"
# Isolate state in a throwaway dir so the smoke run doesn't touch real data.
export CENTRI_DB_PATH="$WORKDIR/centri.db"
export CENTRI_DATA_DIR="$WORKDIR"

cd "$CORE_DIR"
"$PYTHON_BIN" -m uvicorn centri.app:app --host "$HOST" --port "$PORT" --log-level warning \
  >"$SERVER_LOG" 2>&1 &
SERVER_PID=$!

# Wait for /health to come up.
echo "==> Waiting for backend health"
for _ in $(seq 1 50); do
  if curl -sf "$BASE/health" >/dev/null 2>&1; then
    break
  fi
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "FAIL: backend exited during startup" >&2
    cat "$SERVER_LOG" >&2
    exit 1
  fi
  sleep 0.2
done
if ! curl -sf "$BASE/health" >/dev/null 2>&1; then
  echo "FAIL: backend did not become healthy" >&2
  cat "$SERVER_LOG" >&2
  exit 1
fi

# The driver/asserter runs in Python: it opens the WS, sends the utterance,
# collects events, drives the approval over REST, and asserts the round-trip.
echo "==> Driving utterance + asserting streamed events and approval round-trip"
"$PYTHON_BIN" - "$BASE" <<'PYEOF'
import asyncio
import json
import sys
import urllib.request

BASE = sys.argv[1]
WS = BASE.replace("http", "ws", 1) + "/events/stream"


def post(path, body=None):
    data = json.dumps(body).encode() if body is not None else b""
    req = urllib.request.Request(
        BASE + path, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


async def main():
    try:
        import websockets  # type: ignore
    except ImportError:
        print("websockets package not available; falling back to REST polling", file=sys.stderr)
        return await rest_only()

    seen = []
    approved = False
    async with websockets.connect(WS) as ws:
        # Kick off a coding task.
        post("/utterance", {"text": "please refactor the auth module", "source": "desktop_text"})

        async def reader():
            nonlocal approved
            while True:
                raw = await ws.recv()
                ev = json.loads(raw)
                seen.append(ev)
                t = ev.get("type", "")
                if t == "approval.requested" and not approved:
                    approval_id = ev.get("approval_id") or (ev.get("payload") or {}).get("approval_id")
                    if approval_id:
                        post(f"/approvals/{approval_id}/approve")
                        approved = True
                if t in ("task.completed", "task.failed", "task.cancelled"):
                    return

        try:
            await asyncio.wait_for(reader(), timeout=30)
        except asyncio.TimeoutError:
            pass

    assert_events(seen, approved)


async def rest_only():
    # Minimal fallback: drive via REST and poll /events.
    post("/utterance", {"text": "please refactor the auth module", "source": "desktop_text"})
    seen = []
    approved = False
    for _ in range(120):
        events = json.loads(urllib.request.urlopen(BASE + "/events?limit=200", timeout=10).read())["events"]
        seen = events
        for ev in events:
            if ev.get("type") == "approval.requested" and not approved:
                aid = ev.get("approval_id") or (ev.get("payload") or {}).get("approval_id")
                if aid:
                    post(f"/approvals/{aid}/approve")
                    approved = True
        if any(e.get("type") in ("task.completed", "task.failed", "task.cancelled") for e in seen):
            break
        await asyncio.sleep(0.5)
    assert_events(seen, approved)


def assert_events(seen, approved):
    types = [e.get("type", "") for e in seen]
    print(f"==> Observed {len(seen)} events: {sorted(set(types))}")

    def need(pred, label):
        if not any(pred(t) for t in types):
            print(f"FAIL: expected {label} but never saw it", file=sys.stderr)
            print("Events:\n" + "\n".join(types), file=sys.stderr)
            sys.exit(1)

    need(lambda t: t == "task.started", "task.started")
    need(lambda t: t.endswith(".progress"), "a streamed *.progress event")
    need(lambda t: t == "approval.requested", "approval.requested")
    if not approved:
        print("FAIL: approval.requested seen but approval was never POSTed", file=sys.stderr)
        sys.exit(1)
    need(lambda t: t == "approval.resolved", "approval.resolved")
    need(lambda t: t == "task.completed", "task.completed (turn finished after approval)")
    print("==> PASS: command -> task -> streamed events -> approval round-trip verified")


asyncio.run(main())
PYEOF

echo "==> Smoke test passed"
