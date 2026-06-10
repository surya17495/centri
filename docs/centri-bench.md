# centri-bench — a builder-workflow memory benchmark

**Events are the source of truth; memory is a derived, re-derivable index.**

`centri-bench` makes CENTRI's memory claim falsifiable. It is simultaneously a
Phase 2 engineering target, a demo script, and a launch asset. It exists because
no published benchmark measures memory over *sustained building work*: LongMemEval
measures conversational recall and is saturated
([LongMemEval](https://arxiv.org/abs/2410.10813)); the ICLR 2026 memory workshop
has relevant work emerging academically — Proced-Mem, AMA-Bench, ShiftBench — but
nothing shipped or productized
([ICLR 2026 memory workshop](https://iclr.cc/virtual/2026/workshop/10000792)).

This benchmark is the operational form of the
[memory architecture](memory-architecture.md). Read that first for the design it
tests.

## North-star: the zero-spoonfeed test

The benchmark's headline is a direct test of the project's acceptance criterion:

> An agent that has a sense of what exactly we tried, what worked, what didn't,
> what we shouldn't repeat, and what best to do next — without me having to
> spoonfeed context. It should happen automatically.

**Setup.** Seed a multi-week synthetic project history directly into the event
ledger — delegations, diffs, test results, verdicts, abandonments — as the typed
events of [`event-contract.md`](event-contract.md). Then open a **cold-start
session** and issue a deliberately terse instruction, e.g. "improve the
funding-rate signal." No context is pasted; the only memory available is what the
system captured and can re-inject.

**Score three things:**

| Metric                  | Definition                                                                                  | Direction |
|-------------------------|---------------------------------------------------------------------------------------------|-----------|
| Re-proposal rate        | Fraction of runs that re-propose an already-rejected approach. If revisiting, the agent must cite *what changed* | lower is better |
| Brief completeness      | % of relevant decisions / rejections / conventions / open-alternatives auto-included in the hand brief | higher is better |
| Next-step correctness   | Agreement of the proposed next step with a ground-truth plan                                 | higher is better |

Brief completeness is measured against the actual brief CENTRI assembles for the
[`Hand`](../core/src/centri/hands/base.py) — the same cue-driven injection used in
production, not a special benchmark path.

## Task taxonomy

Each task isolates one memory system and the live failure mode it closes in
incumbents.

| # | Task                       | Memory system          | Pass condition                                                                 |
|---|----------------------------|------------------------|--------------------------------------------------------------------------------|
| 1 | Re-proposal avoidance      | Semantic + supersession| Agent does not re-suggest a rejected approach; if it revisits, it states what changed |
| 2 | Episodic recall w/ receipts| Episodic               | "What did we try for X and why did it fail?" answered ordered, causal, with receipt links to events |
| 3 | Stale-fact supersession    | Semantic + supersession| "True now vs true in March" — handles refactored module, changed convention, renamed service |
| 4 | Open-loop surfacing        | Prospective            | Unprompted briefing mentions the loop; dormancy question asked once, not repeatedly |
| 5 | Procedural application     | Procedural             | Applies the user's conventions ("do it the way I always do") without being restated |
| 6 | Silent-abandonment handling| Prospective            | Dormant loop is neither nagged forever nor silently forgotten                  |

Tasks 1 and 3 both exercise supersession but differ in shape: task 1 is about *not
repeating* a rejected path, task 3 is about *reporting current truth* against a
historical one.

## Method

**Ground truth.** Scripted event histories seeded directly into the ledger, plus
scripted user verdicts. Three project personas — a trading system, a web app, an
infra migration — each simulated over 2–6 weeks. Ground-truth plans and the set of
rejected approaches are authored alongside each history.

**Baselines.** The headline comparison is CENTRI-native, but the suite runs a panel:

| Baseline                    | What it tests                                                       |
|-----------------------------|---------------------------------------------------------------------|
| CENTRI native               | The SQLite-schema memory system of [memory-architecture.md](memory-architecture.md) |
| CENTRI + `LettaMemoryStore` | The escape-hatch adapter — native-vs-Letta, same harness            |
| Hermes                      | Incumbent comparison                                                |
| Claude Code                 | Incumbent comparison                                                |
| Cursor                      | Incumbent comparison                                                |

Incumbents receive the same history as transcripts/files wherever their interface
allows. They cannot ingest the typed event ledger, so they are handicapped by
construction — **this handicap is documented honestly** rather than hidden, because
it *is* the point: event-level capture is the differentiator, and the benchmark
should show what an agent loses without it.

**Scoring.** Rubric-graded by an LLM judge with human spot-checks. Report
per-taxonomy scores plus a headline composite. The methodology and harness are
published so the result is reproducible.

## Anti-gaming rule

**The benchmark tasks must be written before Phase 2 implementation begins — and
this document is that commitment.** Authoring the tasks after building the system
would let the implementation quietly target the test. By fixing the taxonomy,
metrics, and method here, before the consolidation worker, supersession, and
cue-driven injection exist, the benchmark stays an honest measure rather than a
self-fulfilling one.

## References

- LongMemEval (saturated conversational recall): <https://arxiv.org/abs/2410.10813>
- Zep temporal knowledge graph: <https://arxiv.org/abs/2501.13956>
- Graphiti: <https://github.com/getzep/graphiti>
- A-MEM: <https://arxiv.org/abs/2502.12110>
- Letta Code: <https://www.letta.com/blog/letta-code>
- Letta — our next phase: <https://www.letta.com/blog/our-next-phase>
- Agent Client Protocol (ACP): <https://agentclientprotocol.com/get-started/introduction>
- ICLR 2026 memory workshop (Proced-Mem, AMA-Bench, ShiftBench): <https://iclr.cc/virtual/2026/workshop/10000792>
