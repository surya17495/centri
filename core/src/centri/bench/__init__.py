"""centri-bench — a builder-workflow memory benchmark.

The operational form of ``docs/centri-bench.md``: makes CENTRI's memory claim
falsifiable by seeding scripted multi-week project histories into the event
ledger and scoring how well each memory backend re-injects context at a
cold-start cue. See :mod:`centri.bench.harness` for the entrypoint (``run`` +
``report``).
"""

from centri.bench.harness import report, run

__all__ = ["run", "report"]
