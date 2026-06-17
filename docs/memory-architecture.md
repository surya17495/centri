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
| **Episodic**    | What happened, in order; what was tried, failed, abandoned | The typed event ledger. Capture at the delegation boundary, at write time |
| **Semantic**    | Durable Decision/Fact objects; "true now vs true in March" | Typed objects with `supersedes` links + provenance receipts into the ledger         |
| **Prospective** | What we still intend to do; open questions; cues           | `OpenLoop` objects + the ported scheduler; loops fire on cue/schedule               |
| **Procedural**  | How the user works ("how I deploy", "how experiments run") | Skill/policy files distilled from repeated episodes; injected into hand briefs      |
| **Working**     | What am I doing *right now* — active task, files, sub-questions | Per-thread key-value store, read before and written after each curation pass   |

### Episodic — the event ledger

Episodic memory is the spine: the `events` table in
[`db.py`](../core/src/centri/db.py), written through `append_event()` after
redaction. The distinguishing move versus incumbents is **where** capture happens.
Chat agents only ever see a transcript. CENTRI captures at the *delegation
boundary* — `task.started`, `task.progress`, `artifact.created`,
`task.completed` / `task.failed`, `hand.completed` / `hand.failed` — so a diff, a
test result, and a verdict are first-class events, not prose a model has to
re-infer later. The required families are enumerated in
[`event-contract.md`](event-contract.md).

**Project-keyed scoping.** Every event is scoped to a *project*, not to a session.
Sessions are an execution detail — the memory boundary is the project. A project
is universal: a coding repo, a research topic, a conversation thread, a person.
The `projects` table (`id, name, kind, ref`) is the universal scoping primitive.
Coding repos are double-bookkept: a `repos` row holds coding-specific metadata
(branch, ahead/behind), and a `projects` row of `kind='repo'` is the scoping
reference. Non-coding work (research, conversations, voice) creates projects of
`kind='topic'`. At ingest time, the adapter resolves a project from the session's
`directory` field (→ repo project) or falls back to the slug/title (→ topic
project), so every event carries a `repo_id` (the project id, kept under the
legacy column name for compatibility). The curation ranker's thread-affinity
feature uses this to give same-project candidates partial credit across sessions.

**Importance filtering.** Every event carries an `importance` field (`low`,
`normal`, `high`) set at ingest time. Tool output and audit noise are `low`;
user utterances, assistant responses, decisions, and session activity are
`normal`. Verbatim FTS recall (`search_events`) defaults to `min_importance='normal'`,
excluding the 90%+ of the spine that is tool output so recall surfaces real
conversation, not shell-command snippets. The consolidation worker also skips
`low`-importance events when promoting to the typed graph — tool output stays in
the spine for provenance but never becomes a graph node. The memory system's own
events (`consolidation.*`, `memory.*` sources) are always excluded from verbatim
recall: they are the system talking to itself, never user-facing context.

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

### Working — what am I doing right now

Working memory bridges turns within a single session without re-deriving state
from the full spine. It is a per-thread key-value store (`working_memory` table)
that holds the current task, active files, and the last utterance. Before each
curation pass, the `Curator.assemble()` reads working memory to recover
active-files and active-task context that the caller didn't supply — so
"continue" or "fix that too" picks up the right files without the user
re-stating them. After the pass, the current utterance and active state are
written back so the next turn inherits them. Entries are discarded when the
thread ends (`clear_working_context`). This is the cognitive-science analog of
working memory: small, fast, and ephemeral, bridging the gap between the
photographic spine and the current moment.

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
- **Importance-filtered promotion.** Events marked `low` importance (tool output,
  audit noise) are skipped during promotion — they stay in the spine for
  provenance but never become graph nodes. Both tiers (deterministic and LLM)
  apply this filter, so the typed graph tracks decisions, facts, and open loops
  derived from substantive interaction only.

### Sleep-time consolidation via a proposal contract (two tiers)

The consolidation worker above is the **deterministic tier**: it reads structured
*synthesis hints* off events (a `fact` / `decision` block on the payload) and folds
them into the graph with no model in the loop. That tier is authoritative and
unchanged.

But most experience arrives as **unhinted** raw text — `hand.stdout`, transcripts,
tool output — with no hint a deterministic rule can key off. To capture it without
confabulation, CENTRI adds a **second, optional LLM tier** governed by a strict
*proposal contract*:

> The model never writes the graph. It only proposes a JSON array of typed ops.
> A deterministic gatekeeper validates each op and decides apply-or-reject, then
> stamps a provenance receipt into the spine.

This is the Letta sleep-time-compute idea ([sleep-time
compute](https://arxiv.org/abs/2504.13171)) bent to fit the "events are truth"
invariant: the LLM is a *proposer*, the deterministic code is the *authority*, and
every write is receipted back to a source event.

**The op schema** (`add_fact`, `add_decision`, `open_loop`, `close_loop`,
`supersede`, `finish`):

| Op             | Required fields                       | Optional       |
|----------------|---------------------------------------|----------------|
| `add_fact`     | `topic`, `statement`                  | `tags`         |
| `add_decision` | `topic`, `statement`                  |                |
| `open_loop`    | `intent`                              |                |
| `close_loop`   | `loop_id` **or** `intent_match`       |                |
| `supersede`    | `node_id`, `kind`, `new_statement`    |                |
| `finish`       | —                                     |                |

**The gatekeeper rejects** — never silently — on: malformed/non-array JSON; an
unknown op; a missing required field; a `supersede` whose target is not a live node
of that kind; a near-duplicate of an existing live node; and **relative-time
language** (`today`, `yesterday`, `recently`, …) in a statement, since a memory
that says "shipped the parser today" rots the moment it is stored. Statements must
carry absolute dates.

**Provenance receipts** are first-class spine events:
`consolidation.proposal.applied` and `consolidation.proposal.rejected` each carry
the op, the `source_event_ids`, the model id, and (on reject) the reason; a
`consolidation.batch` event carries the token usage for the run. The ledger thus
records not just *what* the LLM tier wrote but *why every proposal was accepted or
refused* — auditable after the fact, with token discipline.

**Obsolescence detection.** The consolidation prompt explicitly instructs the
model to examine the live-node digest alongside new activity and emit
`supersede` / `close_loop` ops for any existing decision, fact, or open loop
that is now obsolete — superseded by newer decisions, contradicted by newer
facts, or referencing an abandoned/replaced project or system. This is how the
graph self-cleans without manual cleanup: as the model sees Centri activity
alongside stale HAL decisions in the cross-repo digest, it supersedes them
automatically.

**Cross-repo digest.** The live-node digest shown to the model is **not scoped
by `repo_id`** — it includes ALL live decisions, facts, and open loops across
all repos. This is essential for cross-project obsolescence detection (e.g.
"adopt HAL as memory provider" must be visible when processing Centri events
so the model can supersede it).

**Token budget.** The completion `max_tokens` is 8192 (configurable via
`CENTRI_CONSOLIDATION_MAX_TOKENS`). The prior default of 2048 was too low —
the model hit the ceiling mid-JSON on ~80% of batches, producing truncated
output that the gatekeeper rejected as malformed.

**Triggering.** The tier piggybacks on the consolidation tick. The scheduler stages
unhinted events into a backlog and fires the tier when the backlog reaches a batch
size (`CENTRI_CONSOLIDATION_BATCH_SIZE`, default 8) **or** when a staleness bound is
exceeded, so a slow trickle still gets consolidated. The deterministic tier still
sees the same window and still ignores unhinted events — the two tiers never
double-write because each reads a disjoint slice (hinted vs. unhinted).

**Honest-unavailable.** The tier needs an OpenAI-compatible endpoint
(`CENTRI_CONSOLIDATION_BASE_URL` + `CENTRI_CONSOLIDATION_MODEL`, with the key
injected at runtime). With nothing configured the tier reports unavailable and does
nothing; the deterministic tier is unaffected. A live smoke check lives at
[`scripts/live_consolidation_check.py`](../scripts/live_consolidation_check.py).

**Swappable proposer.** The proposer is a thin client behind `resolve_consolidation_client`.
Because the *contract* (propose ops → gatekeeper applies → receipt) is what is
load-bearing, the proposer is replaceable: a future `LettaConsolidator` that runs
Letta's sleep-time agent could emit the same op array and reuse the same
deterministic gatekeeper unchanged. The schema is the decision; the proposer is
swappable — the same posture as "schema is the decision; the engine is swappable"
above.

> **Note on Decision 3.** The Phase 2 ROADMAP recorded "no LLMs in the consolidation
> loop" to keep synthesis deterministic and cheap. The proposal contract supersedes
> that decision narrowly: LLMs may *propose*, but they still never *write* — the
> deterministic gatekeeper remains the sole authority, so the original intent
> (deterministic, auditable, non-confabulating memory) is preserved while raw
> unhinted experience finally gets captured. The deterministic hint path remains the
> default and runs with no model configured.

### Cue-driven injection (not query-driven retrieval)

Memory is **assembled and pushed**, not waited-for-and-queried. Injection cues are
delegation time, session start, and repo open. At each cue CENTRI assembles the
relevant decisions, rejected approaches, conventions, and open alternatives into
the brief that goes to whatever hand executes the work — through the
[`Hand`](../core/src/centri/hands/base.py) contract, the same brief shape for an
`OpenCodeHand` subprocess or an `AcpHand` JSON-RPC peer.

The brief has two layers: a **cued** layer (ranked retrieval over the typed graph)
and an **ambient** layer (standing context: identity, active projects, top open
loops). A **verbatim** layer adds full-text search matches from the event spine,
filtered to `normal`-importance events and sorted by source priority — user
utterances first, then assistant responses, then session activity, then tool
output — so the most important conversation surfaces above reference material.

**Working memory** is read before the curation pass to recover the active task
and files from the prior turn, and written after with the current utterance and
state. This gives mid-session continuity ("continue", "fix that too") without
re-scanning the full spine each turn.

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
