"""centri-bench CLI entrypoint: ``python -m centri.bench.run``."""

import asyncio
import json
import sys

from centri.bench.harness import report, run


def main() -> None:
    out = asyncio.run(run())
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
