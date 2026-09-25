#!/usr/bin/env python3
"""Run every experiment (or the ones given) against real Bedrock, one
after another, then print a summary table.

    python scripts/run_all.py --dry-run                     # plan + time estimate, no AWS calls
    python scripts/run_all.py                               # all experiments/*.yaml
    python scripts/run_all.py experiments/slo-capacity.yaml experiments/mixed-capacity.yaml
    python scripts/run_all.py --gateway-config my-gateway.yaml

Sequential on purpose -- experiments share the account's Bedrock quota,
so parallel runs would throttle each other. A failed experiment doesn't
stop the rest (unless --fail-fast). Artifacts go to one batch directory
(results/run-all-<timestamp>/ by default) with a summary.yaml.

Exit code: 0 if every experiment succeeded and the gateway diff (if
requested) has no warn findings, else 1.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import yaml  # noqa: E402

from bedrock_benchmark.batch import format_plan, format_summary, plan, run_batch  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("experiments", nargs="*", help="experiment YAMLs (default: experiments/*.yaml)")
    parser.add_argument("--results-dir", help="batch output dir (default: results/run-all-<timestamp>)")
    parser.add_argument("--dry-run", action="store_true", help="validate + print the plan and time estimate, run nothing")
    parser.add_argument("--fail-fast", action="store_true", help="stop at the first failed experiment")
    parser.add_argument("--gateway-config", help="gateway limits snapshot; runs gateway_diff over all produced profiles")
    args = parser.parse_args()

    paths = args.experiments or sorted(str(p) for p in Path("experiments").glob("*.yaml"))
    if not paths:
        print("no experiment files found", file=sys.stderr)
        return 1

    planned = plan(paths)
    print(format_plan(planned))
    if args.dry_run:
        return 0

    gateway_config = None
    if args.gateway_config:
        gateway_config = yaml.safe_load(Path(args.gateway_config).read_text()) or {}

    results_dir = Path(args.results_dir or f"results/run-all-{time.strftime('%Y%m%d-%H%M%S')}")
    batch = run_batch(paths, results_dir=results_dir, fail_fast=args.fail_fast, gateway_config=gateway_config)

    print(f"\n{'=' * 78}\nSUMMARY\n{'=' * 78}")
    print(format_summary(batch))
    return batch.exit_code


if __name__ == "__main__":
    sys.exit(main())
