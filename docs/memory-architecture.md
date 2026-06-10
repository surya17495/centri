# CENTRI Memory Architecture (Phase 2)

**Events are the source of truth; memory is a derived, re-derivable index.**

This document specifies the Phase 2 memory system for CENTRI. Phase 0 shipped the
*substrate* — an append-only event ledger with redaction, a `MemoryStore` ABC, a
minimal `SqliteMemoryStore`, and `rebuild_from_events()` (see
[`memory_store.py`](../core/src/centri/memory_store.py),
[`event-contract.md`](event-contract.md),
[`architecture.md`](architecture.md)). It did **not** ship the memory *system*:
synthesis, supersession, prospective triggers, and cue-driven injection are all
Phase 2. This doc is the spec for that work.

## North-star: the zero-spoonfeed test

The acceptance criterion for the whole memory effort, in the user's words:

> An agent that has a sense of what exactly we tried, what worked, what didn't,
> what we shouldn't repeat, and what best to do next — without me having to
> spoonfeed context. It should happen automatically.

This decomposes into two "automatics":

1. **Capture without manual filing** — the user never files a memory. Everything
   worth remembering is captured at the moment work happens.
2. **Injection without asking** — the user never queries memory. Relevant context
   is assembled and pushed into whatever executes the next piece of work.

Retrieval — the middle step everyone optimizes — is commoditized. LongMemEval is
effectively saturated: local-embedding systems reach ~98% recall@5 with no LLM in
the loop ([LongMemEval](https://arxiv.org/abs/2410.10813)). The differentiation is
not better embeddings; it is **domain ontology + event-level capture at the moment
of experience + ambient retrieval cues**.

The benchmark that makes this falsifiable is [`centri-bench.md`](centri-bench.md).

## Four memory systems

CENTRI models memory after the cognitive-science decomposition. Each system maps
to a concrete mechanism in this codebase and closes a failure mode that incumbents
(transcript-scrolling chat agents) exhibit.

| System          | What it holds                                              | Mechanism in CENTRI                                                                 |
|-----------------|------------------------------------------------------------|-------------------------------------------------------------------------------------|
| **Episodic**    | What happened, in order; what was tried, failed, abandoned | The typed event ledger (Phase 0). Capture at the delegation boundary, at write time |
| **Semantic**    | Durable Decision/Fact objects; "true now vs true in March" | Typed objects with `supersedes` links + provenance receipts into the ledger         |
| **Prospective** | What we still intend to do; open questions; cues           | `OpenLoop` objects + the ported scheduler; loops fire on cue/schedule               |
| **Procedural**  | How the user works ("how I deploy", "how experiments run") | Skill/policy files distilled from repeated episodes; injected into hand briefs      |

### Episodic — the event ledger

Episodic memory already exists as the Phase 0 spine: the `events` table in
[`db.py`](../core/src/centri/db.py), written through `append_event()` after
redaction. The distinguishing move versus incumbents is **where** capture happens.
Chat agents only ever see a transcript. CENTRI captures at the *delegation
boundary* — `task.started`, `task.progress`, `artifact.created`,
`task.completed` / `task.failed`, `hand.completed` / `hand.failed` — so a diff, a
test result, and a verdict are first-class events, not prose a model has to
re-infer later. The required families are enumerated in
[`event-contract.md`](event-contract.md).

### Semantic + supersession

Semantic memory is the set of durable claims about the project: decisions made,
facts established, conventions adopted. Phase 0 represents the leaf of this as
`ArchivalFact` (`id`, `text`, `source_event_id`, `tags`, `created_at`) — note the
`source_event_id`: **every fact carries a receipt** back into the episodic ledger.

Phase 2 adds the typed `Decision` / `Fact` objects with **supersession**: when new
truth arrives, it does not accumulate alongside the old — it *invalidates* the old
with a `supersedes` link and provenance. This is Graphiti-style bi-temporal
invalidation ([Zep](https://arxiv.org/abs/2501.13956),
[Graphiti](https://github.com/getzep/graphiti)), applied not to chat turns but to
*builder state*. The system can answer "what is true now" and "what was true in
March, and what changed it." A renamed service, a refactored module, a reversed
convention each produce a supersession edge, never a contradictory pair of facts
left for the model to reconcile.

### Prospective — open loops

Prospective memory is what the user still intends to do. Phase 2 introduces
`OpenLoop` objects (an intent, its originating event, a cue or schedule, a state).
The Phase 0 [`scheduler.py`](../core/src/centri/scheduler.py) already runs a
periodic tick (`_tick()` calls `poll_once()` and has a "nightly synthesis
placeholder" and "stale task detection" comment); Phase 2 fills those in. Loops
surface in briefings **unprompted** when their cue fires or their schedule comes
due — the user does not have to remember to ask.

### Procedural — distilled skills

Procedural memory is *how* the user works, distilled from repeated episodes:
"how the user deploys", "how experiments are structured". These become
skill/policy files — executable, versioned, injected into hand briefs — inspired
by A-MEM's self-organizing notes ([A-MEM](https://arxiv.org/abs/2502.12110)) and
Letta's git-backed MemFS. A procedure is promoted only after the same pattern is
observed across multiple episodes, so it reflects demonstrated habit, not a single
instance.

## Core mechanisms

### Consolidation worker ("sleep cycle")

A background batch worker folds each window of raw events into typed objects:
attempt/outcome/reason records, `OpenLoop` updates, and skill candidates. It is the
`consume_events` synthesis hook of `MemoryStore` made real, and it emits the
`memory.synthesized` event family already reserved in
[`event-contract.md`](event-contract.md).

Hard rules for the worker:

- **Typed objects with receipts, never freeform prose.** Every synthesized object
  links to the `source_event_id`(s) it was derived from.
- **Conflicts resolve by supersession, never accumulation.** New truth invalidates
  old truth; the ledger retains both, the semantic index reflects only the current.
- **Never confabulate.** If an outcome cannot be attributed to an event, store
  `"outcome unknown"` rather than inventing a result.

### Cue-driven injection (not query-driven retrieval)

Memory is **assembled and pushed**, not waited-for-and-queried. Injection cues are
delegation time, session start, and repo open. At each cue CENTRI assembles the
relevant decisions, rejected approaches, conventions, and open alternatives into
the brief that goes to whatever hand executes the work — through the
[`Hand`](../core/src/centri/hands/base.py) contract, the same brief shape for an
`OpenCodeHand` subprocess or an `AcpHand` JSON-RPC peer.

| Hand kind                          | What injection gives it                                                            |
|------------------------------------|------------------------------------------------------------------------------------|
| Memory-less (OpenCode, Codex)      | Continuity for free — they get project context they could never have held          |
| Memory-ful (Letta Code)            | An advisory bonus — CENTRI supersession state is authoritative about the *project*; the hand's own memory is advisory about the *code* |

This is the inverse of the incumbent pattern, where the user re-pastes context
because the agent only retrieves when asked.

### Dormancy detection

The silent-abandonment failure mode: an open loop is neither nagged forever nor
silently forgotten. When a loop goes untouched for N days, CENTRI surfaces **one**
yes/no line in the next briefing — "still pursuing X, or park it?" — and then drops
it if unanswered. This single line is the *only* spoonfeeding the system is allowed
to ask of the user; everything else must be automatic.

### Hot / warm / cold tiers

Voice and ambient interaction demand sub-100ms context. Memory is tiered:

| Tier | Backing store                          | Budget   | Role                                  |
|------|----------------------------------------|----------|---------------------------------------|
| Hot  | in-process context cache               | <100ms   | Answer from this on the voice path    |
| Warm | SQLite ledger + memory tables          | ms       | Recent events, current semantic index |
| Cold | archival facts / embeddings (sqlite-vec)| async    | Deep recall, consolidated by the worker |

The interactive path answers from hot state; consolidation runs in the background
and never blocks a response. The Phase 0 coordinator already reads hot context on
its fast path (see [`architecture.md`](architecture.md)).

## Storage decision: a graph schema on SQLite, no graph DB

CENTRI stores the semantic + prospective graph as a **schema on SQLite**, not on a
dedicated graph database.

**Why it is enough.** A single user's builder graph is small — on the order of
1e5 nodes/edges after a year. Recursive CTEs traverse that in milliseconds, and
`sqlite-vec` covers embedding search. Graph databases earn their keep at 10M+ facts
in multi-tenant deployments (the regime Zep's published latency tables target), not
at single-seat scale.

**Why it is the right product call.**

| Concern         | SQLite-schema outcome                                                       |
|-----------------|------------------------------------------------------------------------------|
| Desktop footprint| Tauri install ships no JVM, no Neo4j, no Postgres server                    |
| Privacy story    | "Your memory is a SQLite file on your disk" — literally true                |
| Ops cost         | Zero per-seat infrastructure for a bundled-subscription business            |

**Why it is reversible.** Events are the source of truth. If a future cloud or team
tier needs real graph infrastructure, the graph is re-derived into Graphiti by
replaying the ledger. **The schema is the decision; the engine is swappable.**

## Building on Letta: an honest accounting

CENTRI's Phase 0 substrate overlaps Letta's storage/CRUD/retrieval layer, so the
build-vs-adopt question is real. The decision is to build, and the reasons should
be recorded honestly.

**Why not run Letta's server.** Self-hosting Letta means Docker + Postgres +
pgvector — a ~2GB-RAM service. That breaks both the desktop footprint and the
local-first privacy story above. Letta Cloud avoids the install but puts the
user's memory on Letta's infrastructure, stacks our margin on theirs, and exposes
our roadmap to their pivot cadence — tool rules, templates, and the filesystem
feature were deprecated with roughly one month's notice in March 2026
([Letta's next phase](https://www.letta.com/blog/our-next-phase)) — and they ship a
competing consumer app.

**What Letta would and wouldn't save.** Letta saves the commodity ~20% —
storage, CRUD, retrieval — which Phase 0 already built. It saves none of the
expensive ~80%: the domain ontology, supersession, prospective triggers,
cue-driven injection, and consolidation. In Letta those would live as prose in
archival memory anyway, which is exactly what this design rejects.

**The honest gap.** Phase 0 is Letta's *substrate*, not Letta's *system*. Letta is
ahead on agentic memory management (MemGPT-style self-editing memory and
context-pressure management), on sleep-time background refinement, on production
hardening, and on the harness — Letta Code is #1 on Terminal-Bench
([Letta Code](https://www.letta.com/blog/letta-code)). Our closing path is
deliberate, not hand-wavy:

1. **Harness** — rent it, don't rebuild it. Drive external agents over ACP
   ([Agent Client Protocol](https://agentclientprotocol.com/get-started/introduction)),
   so Letta Code itself is welcome as an optional coding hand.
2. **Memory-management loop** — implement from published research (MemGPT,
   sleep-time compute, A-MEM). This is engineering, not research, and our scope is
   narrower than theirs: typed founder-state, not open-ended chat memory.
3. **Ontology + vantage point** — the domain model and the event-level capture
   position that Letta does not have, because it remembers the *worker*, not the
   *work across workers*.

**The escape hatch.** `MemoryStore` is an ABC. Phase 2 writes a `LettaMemoryStore`
adapter (local Docker) and runs `centri-bench` native-vs-Letta head-to-head
([`centri-bench.md`](centri-bench.md)). If Letta wins decisively, we swap the
backend; because events are the source of truth, migration is a re-derivation, not
a rewrite.

## Positioning

- "Events are the source of truth; memory is a derived, re-derivable index."
- "Letta builds the best worker that remembers; CENTRI remembers the work — across
  every worker."

## References

- LongMemEval — conversational recall, saturated: <https://arxiv.org/abs/2410.10813>
- Zep temporal knowledge graph: <https://arxiv.org/abs/2501.13956>
- Graphiti: <https://github.com/getzep/graphiti>
- A-MEM (self-organizing agentic memory): <https://arxiv.org/abs/2502.12110>
- Letta Code: <https://www.letta.com/blog/letta-code>
- Letta — our next phase (pivot/deprecations): <https://www.letta.com/blog/our-next-phase>
- Agent Client Protocol (ACP): <https://agentclientprotocol.com/get-started/introduction>
