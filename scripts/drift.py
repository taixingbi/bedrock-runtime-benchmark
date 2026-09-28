#!/usr/bin/env python3
"""Temporal validation: combine capacity profiles from repeated,
independent runs (different days / times of day) into a stable or
conservative operating envelope -- a single profile is only a
single-run operating envelope:

    python scripts/drift.py results/                      # every profile under results/
    python scripts/drift.py results/run-all-A results/run-all-B --threshold 15

Prints YAML per (model, experiment, workload/mix, sweep kind): a
`temporal_validation` block (runs, days / UTC hours observed, confirmed
min/median/max/spread, conservative values, and which envelope the
evidence supports) plus the runs, oldest first.

Kept for backward compatibility -- the public command is
`bedrock-benchmark validate`, which also writes the
temporal-capacity-profile.yaml artifact (with a per-entry status).
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
    parser.add_argument("--min-runs", type=int, default=3, help="runs needed for a temporal verdict (default 3)")
    parser.add_argument("--min-days", type=int, default=2, help="distinct days needed (default 2)")
    args = parser.parse_args()
    missing = [p for p in args.paths if not Path(p).exists()]
    if missing:
        parser.error(f"not found: {missing}")
    print(yaml.safe_dump(summarize(args.paths, stability_threshold_pct=args.threshold,
                                        min_runs=args.min_runs, min_days=args.min_days), sort_keys=False, width=120))
    return 0


if __name__ == "__main__":
    sys.exit(main())
