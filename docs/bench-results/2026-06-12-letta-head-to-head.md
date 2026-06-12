# Live head-to-head: CENTRI native vs real Letta server (2026-06-12)

Run context: real `letta server` v0.16.8 on embedded Postgres+pgvector; both arms on Nebius Token Factory — agent/consolidation `Qwen/Qwen3-30B-A3B-Instruct-2507`, judge `Qwen/Qwen3-235B-A22B-Instruct-2507`, embeddings `Qwen/Qwen3-Embedding-8B` (4096-d). Native arm: `CENTRI_BENCH_NATIVE_LIVE=1`, embedding weight 0.3 (policy `3c.1-embed`), LLM consolidation tier wired. Reproduce with `scripts/bench_head_to_head.sh`.


Personas: trading, webapp, infra

Both arms grade against the same Nebius Token Factory models. Arm A is CENTRI's typed memory graph with write-time embeddings on; arm B is a real Letta server (archival passages, pgvector similarity, no typed supersession). The thesis: native wins specifically on stale-fact supersession.

## Deterministic rubric

| Metric | centri-native | letta-adapter[letta_http] |
|---|---|---|
| brief completeness ↑ | 1.00 | 1.00 |
| re-proposal rate ↓ | 0.00 | 0.00 |
| next-step correct ↑ | 1.00 | 1.00 |
| stale-fact correct ↑ | 1.00 | 0.67 |
| **composite ↑** | **1.00** | **0.93** |



#### centri-native  (dormancy_ok=True)

| persona | completeness | re-proposal | next-step | stale-fact |
|---|---|---|---|---|
| trading | 1.00 | 0.00 | 1.00 | 1.00 |
| webapp | 1.00 | 0.00 | 1.00 | 1.00 |
| infra | 1.00 | 0.00 | 1.00 | 1.00 |

#### letta-adapter[letta_http]  (dormancy_ok=True)

| persona | completeness | re-proposal | next-step | stale-fact |
|---|---|---|---|---|
| trading | 1.00 | 0.00 | 1.00 | 1.00 |
| webapp | 1.00 | 0.00 | 1.00 | 0.00 |
| infra | 1.00 | 0.00 | 1.00 | 1.00 |


## LLM judge

| Metric | centri-native | letta-adapter[letta_http] |
|---|---|---|
| brief completeness ↑ | 1.00 | 1.00 |
| re-proposal rate ↓ | 0.17 | 0.17 |
| next-step correct ↑ | 1.00 | 1.00 |
| stale-fact correct ↑ | 1.00 | 0.83 |
| **composite ↑** | **0.97** | **0.94** |



#### centri-native  (dormancy_ok=True)

| persona | completeness | re-proposal | next-step | stale-fact |
|---|---|---|---|---|
| trading | 1.00 | 0.50 | 1.00 | 1.00 |
| webapp | 1.00 | 0.00 | 1.00 | 1.00 |
| infra | 1.00 | 0.00 | 1.00 | 1.00 |

#### letta-adapter[letta_http]  (dormancy_ok=True)

| persona | completeness | re-proposal | next-step | stale-fact |
|---|---|---|---|---|
| trading | 1.00 | 0.50 | 1.00 | 1.00 |
| webapp | 1.00 | 0.00 | 1.00 | 0.50 |
| infra | 1.00 | 0.00 | 1.00 | 1.00 |
