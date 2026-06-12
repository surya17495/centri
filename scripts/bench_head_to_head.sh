#!/usr/bin/env bash
# Head-to-head memory benchmark: CENTRI native (arm A) vs a REAL Letta server
# (arm B, letta_http mode), both grading against the same Nebius Token Factory
# models for a fair fight.
#
# This single script stands up everything from a cold machine and runs the
# benchmark end-to-end:
#
#   1. embedded Postgres + pgvector (no Docker) on 127.0.0.1:$PG_PORT, with
#      Letta's ORM schema materialised (scripts/bench_pg_bootstrap.py)
#   2. a real `letta server` (v0.16.x) on 127.0.0.1:$LETTA_PORT, pointed at that DB
#   3. the centri-bench harness, which runs BOTH backends in one pass:
#        - centri-native  (arm A): typed graph + supersession + write-time
#          embeddings ON (CENTRI_BENCH_NATIVE_LIVE=1) + LLM consolidation tier
#        - letta-adapter[letta_http] (arm B): real Letta archival passages,
#          pgvector similarity, NO typed supersession
#   4. results written to $OUT_DIR as JSON (deterministic + judge) plus a
#      markdown comparison.
#
# ---------------------------------------------------------------------------
# NETWORK / AUTH MODEL (read this before running live)
# ---------------------------------------------------------------------------
# Outbound HTTPS to api.tokenfactory.nebius.com is auth-injected by the parent's
# HTTPS proxy *only for clients that honour HTTPS_PROXY* (httpx / requests /
# openai-sdk). We verified the relevant outbound paths use those:
#   - CENTRI embeddings + judge + LLM consolidation -> litellm/httpx  (honour proxy)
#   - Letta server outbound LLM + embeddings        -> openai-sdk (trust_env=True)
# So every model endpoint below is the REAL Nebius base URL, NOT a local relay;
# the proxy injects the bearer token. API-key envs are dummy on purpose — the
# proxy supplies the real credential. If you are NOT running behind that proxy,
# set the *_API_KEY vars to a real Nebius Token Factory key instead.
#
# Usage:
#   scripts/bench_head_to_head.sh                 # full live run
#   OUT_DIR=/tmp/x scripts/bench_head_to_head.sh  # custom output dir
set -euo pipefail

# --------------------------------------------------------------------------
# Paths & ports
# --------------------------------------------------------------------------
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
CORE="$REPO/core"
VENV="$REPO/.venv"

PG_PORT="${PG_PORT:-5432}"
LETTA_PORT="${LETTA_PORT:-8283}"
PGDATA="${PGDATA:-/tmp/centri-bench-pg}"
OUT_DIR="${OUT_DIR:-$REPO/../bench-results}"
LETTA_LOG="${LETTA_LOG:-/tmp/centri-bench-letta.log}"

# --------------------------------------------------------------------------
# Models — IDENTICAL across both arms for fairness
# --------------------------------------------------------------------------
NEBIUS_BASE="${NEBIUS_BASE:-https://api.tokenfactory.nebius.com/v1}"
AGENT_MODEL="${AGENT_MODEL:-Qwen/Qwen3-30B-A3B-Instruct-2507}"
JUDGE_MODEL="${JUDGE_MODEL:-Qwen/Qwen3-235B-A22B-Instruct-2507}"
EMBED_MODEL="${EMBED_MODEL:-Qwen/Qwen3-Embedding-8B}"
EMBED_DIM="${EMBED_DIM:-4096}"
# Dummy key — the auth-injecting proxy supplies the real bearer. Override with a
# real Nebius key if running without the proxy.
NEBIUS_KEY="${NEBIUS_KEY:-proxy-injects-real-key}"

# --------------------------------------------------------------------------
# NO_PROXY — keep loopback + NLTK off the auth-injecting HTTPS proxy
# --------------------------------------------------------------------------
# The parent's auth proxy intercepts ALL outbound HTTP(S) for proxy-honouring
# clients. Two startup paths break under that:
#   - the localhost health curl (and any 127.0.0.1/Letta-server traffic) gets
#     hijacked and returns proxy garbage instead of reaching our local server;
#   - Letta's NLTK "data availability" check downloads from
#     raw.githubusercontent.com at boot; through the proxy it receives a TLS
#     alert record and hangs at "Checking NLTK data availability".
# Exclude both from proxying. asyncpg ignores proxy env entirely, so the DB
# path is unaffected either way; this only steers HTTP clients. Real model
# traffic to api.tokenfactory.nebius.com is deliberately NOT excluded — it must
# still flow through the proxy to get its bearer injected.
NO_PROXY_LIST="127.0.0.1,localhost,::1,raw.githubusercontent.com"
export NO_PROXY="${NO_PROXY:+$NO_PROXY,}$NO_PROXY_LIST"
export no_proxy="${no_proxy:+$no_proxy,}$NO_PROXY_LIST"

mkdir -p "$OUT_DIR"

# --------------------------------------------------------------------------
# Activate venv
# --------------------------------------------------------------------------
# shellcheck disable=SC1091
source "$VENV/bin/activate"

cleanup() {
  echo "== teardown =="
  # Stop the Letta server if we started it.
  if [[ -n "${LETTA_PID:-}" ]]; then
    kill "$LETTA_PID" 2>/dev/null || true
    wait "$LETTA_PID" 2>/dev/null || true
  fi
  # Stop Postgres.
  python "$HERE/bench_pg_bootstrap.py" down "$PGDATA" 2>/dev/null || true
}
trap cleanup EXIT

# --------------------------------------------------------------------------
# 1) Postgres + pgvector + Letta ORM schema
# --------------------------------------------------------------------------
echo "== [1/3] embedded Postgres + pgvector + Letta schema (port $PG_PORT) =="
python "$HERE/bench_pg_bootstrap.py" up "$PGDATA" "$PG_PORT"

# --------------------------------------------------------------------------
# 2) Real Letta server against that DB
# --------------------------------------------------------------------------
echo "== [2/3] booting real Letta server (port $LETTA_PORT) =="
# Postgres engine; sslmode=disable because the bundled pgserver postmaster is
# not built with SSL and Letta otherwise defaults connect_args to ssl=require.
export LETTA_PG_URI="postgresql+pg8000://postgres@127.0.0.1:${PG_PORT}/letta?sslmode=disable"
# Letta's outbound OpenAI client needs *a* key present to initialise; the proxy
# swaps in the real bearer for api.tokenfactory.nebius.com.
export OPENAI_API_KEY="${OPENAI_API_KEY:-$NEBIUS_KEY}"

letta server --port "$LETTA_PORT" --host 127.0.0.1 > "$LETTA_LOG" 2>&1 &
LETTA_PID=$!

# Wait for health (or die with the log tail).
for _ in $(seq 1 60); do
  if curl -sf "http://127.0.0.1:${LETTA_PORT}/v1/health/" >/dev/null 2>&1; then
    break
  fi
  if ! kill -0 "$LETTA_PID" 2>/dev/null; then
    echo "Letta server exited during boot. log tail:"; tail -30 "$LETTA_LOG"; exit 1
  fi
  sleep 1
done
if ! curl -sf "http://127.0.0.1:${LETTA_PORT}/v1/health/" >/dev/null 2>&1; then
  echo "Letta server did not become healthy. log tail:"; tail -30 "$LETTA_LOG"; exit 1
fi
echo "Letta server healthy on 127.0.0.1:${LETTA_PORT}"

# --------------------------------------------------------------------------
# 3) Run the bench harness (both arms in one pass)
# --------------------------------------------------------------------------
echo "== [3/3] running centri-bench (native arm A + real Letta arm B) =="
cd "$CORE"
export PYTHONPATH="src:${PYTHONPATH:-}"

# ---- Arm A (native) live config: embeddings ON + LLM consolidation tier ----
export CENTRI_BENCH_NATIVE_LIVE=1
export CENTRI_EMBEDDING_ENABLED=true
export CENTRI_EMBEDDING_MODEL="$EMBED_MODEL"
export CENTRI_CURATION_W_EMBEDDING_SIMILARITY="${CENTRI_CURATION_W_EMBEDDING_SIMILARITY:-0.3}"
# CENTRI embeddings route through litellm (proxy-honouring) at the real base URL.
export LITELLM_BASE_URL="$NEBIUS_BASE"
export LITELLM_API_KEY="$NEBIUS_KEY"
# LLM consolidation tier (httpx, proxy-honouring).
export CENTRI_CONSOLIDATION_BASE_URL="$NEBIUS_BASE"
export CENTRI_CONSOLIDATION_MODEL="$AGENT_MODEL"
export CENTRI_CONSOLIDATION_API_KEY="$NEBIUS_KEY"

# ---- Arm B (real Letta) config ----
export CENTRI_LETTA_URL="http://127.0.0.1:${LETTA_PORT}"
export CENTRI_LETTA_MODEL="$AGENT_MODEL"
export CENTRI_LETTA_MODEL_ENDPOINT="$NEBIUS_BASE"
export CENTRI_LETTA_EMBEDDING_MODEL="$EMBED_MODEL"
export CENTRI_LETTA_EMBEDDING_ENDPOINT="$NEBIUS_BASE"
export CENTRI_LETTA_EMBEDDING_DIM="$EMBED_DIM"

# ---- LLM judge config (httpx, proxy-honouring) ----
export CENTRI_JUDGE_BASE_URL="$NEBIUS_BASE"
export CENTRI_JUDGE_MODEL="$JUDGE_MODEL"
export CENTRI_JUDGE_API_KEY="$NEBIUS_KEY"

# Offline smoke mode: prove pg + Letta bring-up AND the exact request path that
# 500'd in the live run — actor lookup (async asyncpg session) + a real passage
# INSERT via letta_client — WITHOUT any outbound Nebius traffic. We point the
# agent's embedding endpoint at a tiny local fake (127.0.0.1, in NO_PROXY) that
# returns a constant 4096-d vector, so the server does its real DB work and its
# real embedding HTTP call, but no model token is ever spent. Set BENCH_DRYRUN=1
# to use it — this is how the orchestration is validated before a live run.
if [[ "${BENCH_DRYRUN:-}" == "1" ]]; then
  echo "-- DRY RUN: local fake-embedding endpoint + real actor lookup + passage INSERT --"
  python - <<PY
import os, json, threading, time, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

EMB_DIM = int(os.environ["CENTRI_LETTA_EMBEDDING_DIM"])
EMB_PORT = int(os.environ.get("FAKE_EMBED_PORT", "8911"))
EMB_URL = f"http://127.0.0.1:{EMB_PORT}/v1"

# --- tiny OpenAI-compatible /v1/embeddings server (constant vector) ----------
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silence
        pass
    def do_POST(self):
        n = int(self.headers.get("content-length", 0))
        body = self.rfile.read(n) if n else b"{}"
        try:
            req = json.loads(body or b"{}")
            inp = req.get("input", "")
            count = len(inp) if isinstance(inp, list) else 1
        except Exception:
            count = 1
        vec = [0.001] * EMB_DIM
        data = [{"object": "embedding", "index": i, "embedding": vec} for i in range(count)]
        payload = json.dumps({
            "object": "list", "data": data,
            "model": req.get("model", "fake") if isinstance(req, dict) else "fake",
            "usage": {"prompt_tokens": 0, "total_tokens": 0},
        }).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

srv = ThreadingHTTPServer(("127.0.0.1", EMB_PORT), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(0.2)

# --- drive the exact failing path through letta_client ----------------------
from centri.letta_http import LettaHTTPClient
c = LettaHTTPClient(
    base_url=os.environ["CENTRI_LETTA_URL"],
    model=os.environ["CENTRI_LETTA_MODEL"],
    model_endpoint=os.environ["CENTRI_LETTA_MODEL_ENDPOINT"],  # never called (no agent step)
    embedding_model=os.environ["CENTRI_LETTA_EMBEDDING_MODEL"],
    embedding_endpoint=EMB_URL,                                # local fake, no Nebius
    embedding_dim=EMB_DIM,
)
aid = c.ensure_agent()                  # agent create -> actor lookup (async asyncpg)
c.insert_passage("dry-run probe: authsvc renamed to identity-gateway",
                 tags=["dryrun"])       # actor lookup + embedding(fake) + passage INSERT
got = c.search_passages("identity-gateway", limit=5)   # actor lookup + similarity read
assert any("identity-gateway" in t for t, _ in got), f"passage not retrievable: {got!r}"
c.reset()
srv.shutdown()
print(f"dry-run OK: agent {aid} actor-lookup + passage INSERT + similarity read "
      f"round-tripped against the live server ({len(got)} passage(s) read back)")
PY
  echo "Dry run complete; skipping live harness."
  exit 0
fi

echo "-- deterministic rubric --"
python -m centri.bench.run --json > "$OUT_DIR/results_deterministic.json"
python -m centri.bench.run        > "$OUT_DIR/report_deterministic.txt"

echo "-- LLM judge --"
python -m centri.bench.run --judge --json > "$OUT_DIR/results_judge.json"
python -m centri.bench.run --judge        > "$OUT_DIR/report_judge.txt"

# --------------------------------------------------------------------------
# Markdown comparison
# --------------------------------------------------------------------------
echo "== writing markdown comparison =="
python "$HERE/bench_compare_md.py" \
  "$OUT_DIR/results_deterministic.json" \
  "$OUT_DIR/results_judge.json" \
  > "$OUT_DIR/comparison.md"

echo
echo "Done. Artifacts in: $OUT_DIR"
ls -1 "$OUT_DIR"
