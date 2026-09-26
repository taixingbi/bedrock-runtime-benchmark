#!/usr/bin/env python3
"""Compare capacity profiles from repeated runs to see how stable each
statistically confirmed envelope is over time (provider drift):

    python scripts/drift.py results/                      # every profile under results/
    python scripts/drift.py results/run-all-A results/run-all-B --threshold 15

Prints YAML per (model, experiment, workload/mix, sweep kind): runs,
days observed, confirmed/production values over time, min/median/max,
spread and a conservative (minimum) production value.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import yaml  # noqa: E402

from bedrock_benchmark.drift import summarize  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="+", help="capacity-profile.yaml files or directories to search")
    parser.add_argument("--threshold", type=float, default=20.0,
                        help="max spread (%%) of confirmed values to call an envelope stable (default 20)")
    args = parser.parse_args()
    missing = [p for p in args.paths if not Path(p).exists()]
    if missing:
        parser.error(f"not found: {missing}")
    print(yaml.safe_dump(summarize(args.paths, stability_threshold_pct=args.threshold), sort_keys=False, width=120))
    return 0


if __name__ == "__main__":
    sys.exit(main())
