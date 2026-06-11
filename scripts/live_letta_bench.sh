#!/usr/bin/env bash
# Live head-to-head bench against a REAL Letta server (letta_http mode).
#
# Prereqs (already stood up in the build sandbox; see README honest-accounting):
#   - Embedded Postgres + pgvector on 127.0.0.1:5432 (db "letta")
#   - `letta server` running on 127.0.0.1:8283, configured to use the relay
#     for both LLM and embeddings
#   - The keyless relay at 127.0.0.1:8901 actually serving (chat + embeddings)
#
# Usage: scripts/live_letta_bench.sh
set -euo pipefail
cd "$(dirname "$0")/../core"

export CENTRI_LETTA_URL="${CENTRI_LETTA_URL:-http://127.0.0.1:8283}"
export CENTRI_LETTA_MODEL_ENDPOINT="${CENTRI_LETTA_MODEL_ENDPOINT:-http://127.0.0.1:8901/v1}"
export CENTRI_LETTA_EMBEDDING_ENDPOINT="${CENTRI_LETTA_EMBEDDING_ENDPOINT:-http://127.0.0.1:8901/v1}"
export PYTHONPATH="src:${PYTHONPATH:-}"

echo "== deterministic =="
python -m centri.bench.run

echo
echo "== llm judge =="
python -m centri.bench.run --judge
