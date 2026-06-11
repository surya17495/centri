"""centri-bench CLI entrypoint: ``python -m centri.bench.run``.

Flags:
  --json    machine-readable scores
  --judge   grade with the LLM judge (CENTRI_JUDGE_* env) instead of the
            deterministic rubric. The judge is installed via set_judge() so the
            harness path is identical; only the grader differs.
"""

import asyncio
import json
import sys

from centri.bench.harness import report, run
from centri.bench.scoring import set_judge


def main() -> None:
    if "--judge" in sys.argv:
        from centri.bench.judge import make_judge

        set_judge(make_judge())
    try:
        out = asyncio.run(run())
    finally:
        set_judge(None)
    if "--json" in sys.argv:
        payload = {
            "personas": out["personas"],
            "results": [
                {"backend": r.backend, "headline": r.headline(), "dormancy_ok": r.dormancy_ok,
                 "per_persona": [vars(s) for s in r.scores]}
                for r in out["results"]
            ],
        }
        print(json.dumps(payload, indent=2))
    else:
        print(report(out))


if __name__ == "__main__":
    main()
