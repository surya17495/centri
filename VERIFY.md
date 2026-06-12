# VERIFY — owner's real-machine checklist (Phase 0 gate)

Everything in CENTRI is fixture-verified in CI, but a pile of claims can only be
*honestly* closed on a real laptop with real data and real binaries: the ACP
`opencode` binary, the user's actual OpenCode/Claude Code/Cursor stores, the
Tauri build toolchain, and a live `models.dev` fetch. This is the owner's ~1-hour
pass that turns "fixture-verified only" into "verified on real machine."

**How to use this:** run the six steps in order on your own machine. Each step
states the exact commands, the expected result, **what to record into HANDOFF.md**
(the "Verified on real machine" table), and **which honesty caveat it clears**.
macOS-first; Linux notes inline. If a step fails, record `no` + the failure — a
recorded failure is a successful verification of *where we actually are*.

Conventions assumed throughout: the core runs on `http://127.0.0.1:8760`
(deploy default; see `deploy/README.md`). If you started it on the dev port
`8787` instead, substitute that. If `CENTRI_AUTH_TOKEN` is set, add
`-H "Authorization: Bearer $CENTRI_AUTH_TOKEN"` to every curl (the install
script prints the token); empty token = no header needed. `jq` is handy but
optional.

---

## Step 1 — Install, start core + shell, confirm health

Bring the system up the way a new user would, from the deploy docs.

```bash
# Backend (from repo root). LITELLM_* point at your provider transport;
# the fake values below let everything report "configured" for a dry run.
cd core
env LITELLM_BASE_URL=http://127.0.0.1:4999/v1 LITELLM_API_KEY=test-proxy-key \
    CENTRI_AUTONOMY_LEVEL=supervised \
    python -m uvicorn centri.app:app --host 127.0.0.1 --port 8760 &

# Shell (separate terminal, from repo root)
cd shell
npm install
npm run dev -- --host 127.0.0.1 --port 1420
# In the shell Settings → Backend: set URL http://127.0.0.1:8760 (+ token if set).

# Health probe
curl -s http://127.0.0.1:8760/health        # → {"status":"ok","version":"0.1.0"}
curl -s http://127.0.0.1:8760/status | jq    # role_models populated, hands listed
```

- **Expected:** `/health` returns `{"status":"ok",...}`; the shell loads and shows
  an (empty) timeline; `/status` lists hands and `role_models`.
- **Record in HANDOFF:** `Step 1 install + boot` → yes/no + the version string and
  whether the shell connected.
- **Clears caveat:** "needs-local-build" for the *core+shell boot path* — proves
  the documented quick-start actually starts on a real OS.
- **Linux note:** identical; on Ubuntu use the `deploy/install.sh` path instead of
  the manual uvicorn line if you want the systemd/Caddy bundle.

---

## Step 2 — First-run onboarding: discovery + bootstrap + receipts

Prove discovery finds *real* coding-agent data on disk and that imported memories
carry receipts.

```bash
# Read-only discovery — counts only, no import:
curl -s http://127.0.0.1:8760/ingest/discover | jq
# → sources: [{agent:"opencode",count:N,...},{agent:"claude_code",...},{agent:"cursor",...}]

# Full one-time import (emits ingest.bootstrap.* on the timeline):
curl -s -X POST http://127.0.0.1:8760/ingest/bootstrap \
     -H 'Content-Type: application/json' -d '{}' | jq
# → {"imported":N,"source_count":K,...}

# Spot-check 3 imported memories carry a source_event_id receipt:
curl -s "http://127.0.0.1:8760/memory/graph" | jq '.facts[:3] | .[] | {statement, source_event_id}'
```

- **Expected:** discovery reports non-zero counts for at least one real agent you
  actually use; bootstrap imports them and the shell timeline shows
  `ingest.bootstrap.started/progress/completed`; each spot-checked fact has a
  non-empty `source_event_id` pointing at a real ingested event. Re-running
  bootstrap imports **0** new (idempotent).
- **Record in HANDOFF:** `Step 2 discovery + bootstrap` → yes/no + per-agent counts
  found, total imported, and whether the 3 receipts resolved.
- **Clears caveat:** "Claude Code / Cursor adapters are *fixture-verified only*"
  (HANDOFF 3b.4/3b.5) — this is the first time the readers meet real on-disk
  schemas across release versions.
- **Linux note:** default probe paths differ by platform; if discovery shows 0 for
  an agent you do use, set `CENTRI_INGEST_<AGENT>_PATHS` to its store and re-probe
  before declaring failure.

---

## Step 3 — ACP smoke with the real `opencode acp` binary

Prove the default coding hand drives a real ACP binary end-to-end, not just the fake.

```bash
which opencode                # confirm the real binary is on PATH
# Default hand is already acp_command="opencode acp", acp first in hand_priority.

# Ask for a real coding task through the shell (or curl):
curl -s -X POST http://127.0.0.1:8760/utterance \
     -H 'Content-Type: application/json' \
     -d '{"text":"create a file hello.txt containing the word centri in <some repo>"}' | jq
# Approve the coding card in the shell (supervised mode).

# After it runs, confirm the transcript landed and consolidation folded a fact:
curl -s "http://127.0.0.1:8760/events?limit=80" | jq '.events[] | select(.type=="hand.transcript") | .id'
curl -s "http://127.0.0.1:8760/memory/graph" | jq '.facts[] | select(.topic|test("delegated-session"))'
```

- **Expected:** the task runs to completion via the ACP hand; a `hand.transcript`
  event lands with the full (untruncated) text; on the next consolidation tick a
  `fact` with `topic: delegated-session:<uid>` (tags `[hand, transcript, acp]`)
  appears in the graph with a receipt; the file change actually happened on disk.
- **Record in HANDOFF:** `Step 3 real ACP coding task` → yes/no + the binary version
  (`opencode --version`), whether the transcript event and folded fact appeared.
- **Clears caveat:** "Real-binary ACP verification still pending on a real machine"
  (HANDOFF Decision 2) — the single biggest open honesty gap.
- **Linux note:** identical; ensure `opencode` is installed and authenticated
  (Step 4 covers provider auth).

---

## Step 4 — Provider reuse (single LLM config)

Prove CENTRI reuses OpenCode's configured providers without you re-entering keys.

```bash
# With OpenCode already authenticated on this machine:
curl -s http://127.0.0.1:8760/providers/discovered | jq
# → {available:true, providers:[{id:"anthropic",has_key:true},...]}
```

- **Expected:** providers OpenCode has configured show up with `has_key: true`,
  **and you never typed a key into CENTRI**. Key *material* is never returned —
  only `has_key`. With no `CENTRI_*` env keys set, `/status` `role_models` should
  still resolve via the OpenCode-auth fallback.
- **Record in HANDOFF:** `Step 4 provider reuse` → yes/no + which providers were
  reused and whether any key had to be re-entered (should be none).
- **Clears caveat:** "OpenCode auth/config formats are *fixture-verified only*"
  (HANDOFF 3b.5) — real `auth.json`/`opencode.json` shapes across releases.
- **Linux note:** identical; config probe paths (`~/.config/opencode`,
  `~/.local/share/opencode`) are already platform-aware.

---

## Step 5 — Curation sanity: recall referencing yesterday's real history

Prove the brief pulls the right context from real history with receipts, and that
the *remembering is invisible* in the UI.

```bash
# Ask something that depends on real prior work (ideally from a previous day):
curl -s -X POST http://127.0.0.1:8760/utterance \
     -H 'Content-Type: application/json' \
     -d '{"text":"what did we decide about <a topic from your real history>?"}' | jq

# Inspect the brief receipts on the spine (NOT shown to the user in the UI):
curl -s "http://127.0.0.1:8760/events?limit=40" | jq '.events[] | select(.type=="curation.brief") | .payload'
```

- **Expected:** the answer reflects real prior history; the `curation.brief` event
  carries per-line score breakdowns + `source_event_id` receipts and a
  `tokenizer_stamp` (`tiktoken:o200k_base`, or `wordcount:v1` only if tiktoken is
  genuinely unavailable); the shell shows **no** retrieval mechanics — no "I
  remember…", no visible ranking. Receipts are available on demand, invisible by
  default.
- **Record in HANDOFF:** `Step 5 curation recall` → yes/no + whether receipts
  resolved, the tokenizer stamp observed, and that remembering stayed invisible.
- **Clears caveat:** confirms "no visible remembering" (Decision 8) and real-token
  budgeting (Decision 7 / 3c.0.1) hold against real history, not just fixtures.
- **Linux note:** identical.

---

## Step 6 — Tauri build + live models.dev fetch

Prove the desktop binary builds and the model catalog fetches live; note failures
plainly.

```bash
# Tauri build (from shell/). Requires the Rust toolchain + platform deps.
cd shell
npm run tauri build        # macOS: produces a .app/.dmg under src-tauri/target/release/bundle

# Live models.dev fetch (forces a network refresh, bypassing warm cache):
curl -s "http://127.0.0.1:8760/models/catalog?refresh=true" | jq '{available, count}'
```

- **Expected:** the Tauri build either produces a runnable bundle or fails with a
  recorded reason (missing Rust/toolchain/signing); `/models/catalog?refresh=true`
  returns `available:true` with a non-trivial `count` when online, and
  honest-unavailable (`available:false` + reason) when offline.
- **Record in HANDOFF:** `Step 6 Tauri build + models.dev` → yes/no + the build
  outcome (bundle path or failure reason) and the live catalog count/availability.
- **Clears caveat:** "needs-local-build (Tauri binary)" and "models.dev live fetch"
  (HANDOFF rule 3) — the catalog is a soft dependency, so an offline failure here
  is acceptable and should be recorded as such, not treated as a blocker.
- **Linux note:** Tauri needs `webkit2gtk`/`libsoup` dev packages; if the build
  fails for missing system libs, record the exact missing package rather than
  marking the whole step failed.

---

## After the pass

Fill in the "Verified on real machine" table in `HANDOFF.md` (one row per step,
yes/no/notes) and commit it. Any `no` row stays an open item on the Phase 0 gate —
demo claims that depend on an unverified step must remain hedged until that row
flips to `yes`.
