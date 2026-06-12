# CENTRI — Vision & Master Plan

This is the **why** and the **end-to-end shape** of CENTRI. `docs/ROADMAP.md` is
the **what/when** (phases, tasks, ratified decisions); `HANDOFF.md` is the
**continuity mechanism** a fresh agent reads first. This document is the map the
roadmap is a route through — it changes rarely and only by owner ratification.

Owner: surya. Repo: https://github.com/surya17495/centri.

---

## North star (ratified)

**"OpenCode with photographic memory" is the wedge; the trajectory is Jarvis.**

- **Photographic memory.** The agent remembers everything we did — decisions,
  rejections, conventions, open loops, the narrative of the work — and pulls the
  right context *before the user has to ask*. State lives in an append-only event
  ledger and a derived, re-derivable graph, never in conversation buffers.
  **Photographic storage, human recall** (Decision 13, ratified 2026-06-12): the
  spine is photographic (append-only, nothing deleted, everything re-derivable);
  recall is human (gist-first curated briefs with zoom-in-on-demand that never
  fails, because every gist line carries a `source_event_id` receipt to verbatim
  ground truth). Forgetting is a *read-time presentation policy*, never write-time
  deletion — tiered digests are the gist layer over a lossless spine.
  *The sentence: remembers everything verbatim, recalls like a person, verifies
  like a machine.*
- **Shared-core continuity.** One memory and one server behind every client
  (desktop / web / mobile). Separation lives only in the chat UI. **No sync
  layer, no offline cache, no conflict resolution** unless a hosted/offline
  future forces it.
- **Coding-first.** OpenCode-over-ACP is the default coding hand; every other
  agent (Cursor, Claude Code, …) is a config entry identified by a launch
  command, not new code.
- **Automations.** Tools are a first-class contract parallel to Hand: every
  invocation is an event with receipts; side-effectful tools are approval-gated;
  their output is ingestible by consolidation.
- **Self-hosted now, hosted later.** Single-tenant on the user's own machine is
  the dominant case today. Hosted multi-tenant comes only after the wedge is
  proven publicly — but the structural prerequisites (tenancy key, redaction,
  receipts) are paid for now while they are cheap.
- **Voice.** Voice input on the existing event/cue pipeline; local-first STT with
  a pinned model.

### The scope test

For every proposed feature ask: **does this make us a better
OpenCode-with-memory, or is it premature Jarvis?** If it is the latter, it waits.
This single test arbitrates roadmap disputes.

---

## Sequenced phases

The phases below **replace and absorb** the older `3b/3c/3d/3e` framing. The old
numbers are kept as a compatibility mapping so existing HANDOFF/work-queue
references resolve.

| Phase | Name | Old mapping |
|-------|------|-------------|
| 0 | Real-machine verification | new (gate) — see `VERIFY.md` |
| 1 | Memory completion | 3c.1 + 3e |
| 2 | Feels human | 3d |
| 3 | Hands & surfaces | 3b multi-channel remainder + 3a deploy |
| 4 | Automations | new |
| 5 | Voice | old "Phase 4 — Voice" |
| 6 | Hosted | old "Phase 5 — Productization" |

### Phase 0 — Real-machine verification (owner-run gate)

Everything verified only against fixtures must be re-verified on a real laptop
before it backs a demo claim. The owner runs the ~1-hour checklist in
`VERIFY.md` and records results into the HANDOFF "Verified on real machine"
table. This phase gates demo/marketing claims, not further development — agents
keep building while the owner clears caveats.

### Phase 1 — Memory completion (was 3c.1 + 3e)

The retrieval engine, made complete and measurable.

- **TEMPR-shaped retrieval.** Four parallel **deterministic** retrievers over the
  spine/graph — lexical (BM25-ish), graph-hop, temporal, and stored-vector
  semantic — fused with **Reciprocal Rank Fusion** (pure arithmetic). No LLM
  judgment at read time. An optional pinned, local cross-encoder **reranker** may
  sit behind a policy-stamped seam; it never invents, only reorders.
- **Write-time embeddings.** Embeddings computed when a candidate is written, with
  the embedding model **pinned and recorded in the policy stamp** (like the
  tokenizer stamp). Stored-vector similarity then slots into the fuser as pure
  arithmetic at read time. The `Candidate.vector` slot is already shaped for this.
- **Replay harness.** Re-run any curation policy over historical turns and score
  miss/waste against the `curation.miss` / `curation.waste` ledger — so a policy
  change is measured, not asserted.
- **Quality-per-token bench.** Precision/recall of the facts a turn actually
  needed, per token spent — the headline 3c metric.
- **External baselines.** Letta is benched (native 1.00 vs Letta 0.93). Add
  **Graphiti** and **Hindsight** as comparison baselines.
- **Opinions-with-confidence.** A new typed memory class: the agent's own
  stances, each carrying a confidence and a receipt — distinct from observed
  facts and from user decisions.

### Phase 2 — Feels human (was 3d)

Proactivity on Phase-1 machinery, no new retrieval engine.

- **Prose ambient narrative** — an LLM at *write* time turns the standing digest
  into readable prose (with receipts; deterministic fallback when unconfigured).
- **Waking-up briefing** — unprompted situating on the first interaction of a
  day/session ("while you were away…").
- **Spontaneous association** — a threshold on ranker scores surfaces "this is
  like X from before" as an aside.
- **Dormancy nudges** — the open-loop scheduler surfaces stale intents per a
  policy window.

### Phase 3 — Hands & surfaces

Prove the coding wedge on real binaries and ship the clients.

- **Real-binary ACP verification** against the actual `opencode acp` binary.
- **Second hand demo** — Claude Code via ACP, config only (no new code).
- **Tauri binary build**; **web deploy** of the same React shell; **PWA
  manifest** for mobile. Shared-core continuity, **no sync layer** (ratified).

### Phase 4 — Automations

- **Tool abstraction** parallel to Hand: event-sourced, approval-gated, receipts;
  tool output ingestible by consolidation (see Decision in ROADMAP).
- **Playwright browser tool** is the first Tool.
- Scheduled / proactive task execution flows through the **same approval gate**.

### Phase 5 — Voice

- **Input first.** Local STT (pinned model) → `user.utterance` events → the
  existing cue pipeline. No new memory path — voice is just another way to
  produce utterances.
- **Transport:** WebSocket audio frames into the existing event-socket family for
  v1 (see Decision). TTS later.

### Phase 6 — Hosted

- Tenancy **enforcement** on every query path (the tenancy *key* is laid down
  now in Phase A; enforcement is here). Real authn/z, billing.
- Only after the wedge is proven publicly.

---

## Execution model

- **Agents drive phase-by-phase.** Each unit of work is one commit pushed to
  `main` (no PRs); pushed code is the only safe code.
- **Owner checkpoints** at phase boundaries and before any irreversible or
  external action (deploys, real-machine verification, public launch).
- **`HANDOFF.md`** is the continuity mechanism between agent sessions — always
  current, updated in the same commit as the work it describes.
- **Determinism, receipts, honest-unavailable seams, redaction** are invariants
  across every phase, not features of one.
