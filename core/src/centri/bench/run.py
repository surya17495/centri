"""centri-bench CLI entrypoint: ``python -m centri.bench.run``.

Flags:
  --json               machine-readable scores
  --judge              grade with the LLM judge (CENTRI_JUDGE_* env) instead of
                       the deterministic rubric. The judge is installed via
                       set_judge() so the harness path is identical; only the
                       grader differs.
  --suite continuity   run the 3e continuity regression gate (cross-session
                       awareness, supersession under churn, cold-start recall,
                       delegated-session awareness) instead of the headline
                       persona panel. Honest pass/fail; failures are findings.
"""

import asyncio
import json
import sys

from centri.bench.harness import report, run
from centri.bench.scoring import set_judge


def _suite_arg() -> str:
    if "--suite" in sys.argv:
        i = sys.argv.index("--suite")
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return "headline"


def _run_continuity() -> None:
    from centri.bench.continuity import report_continuity, run_continuity

    result = asyncio.run(run_continuity())
    if "--json" in sys.argv:
        print(json.dumps({
            "suite": "continuity",
            "pass_rate": result.pass_rate(),
            "scores": [vars(s) for s in result.scores],
        }, indent=2))
    else:
        print(report_continuity(result))


def main() -> None:
    if _suite_arg() == "continuity":
        _run_continuity()
        return

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
