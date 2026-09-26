#!/usr/bin/env python3
"""Run every experiment against every model in catalog/models.yaml, one
after another, then print a summary table.

    python scripts/run_all.py --dry-run                        # plan + time estimate, no AWS calls
    python scripts/run_all.py                                  # all experiments x all enabled models
    python scripts/run_all.py --model nova-micro --model nova-pro
    python scripts/run_all.py experiments/rate-capacity.yaml   # one experiment, all models
    python scripts/run_all.py --model nova-micro --slo-profile gold   # only gold workloads
    python scripts/run_all.py --gateway-config my-gateway.yaml
    python scripts/run_all.py --model nova-micro --pilot         # ~30 s smoke test, no batch

Sequential on purpose -- runs share each model's Bedrock quota, so
parallel runs would throttle each other. A failed run doesn't stop the
rest (unless --fail-fast). Results go to results/run-all-<timestamp>/
with one folder per model and a summary.yaml.

Exit code: 0 if every run succeeded and the gateway diff (if requested)
has no warn findings, else 1.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import yaml  # noqa: E402

from bedrock_benchmark.batch import format_plan, format_summary, plan, run_batch  # noqa: E402
from bedrock_benchmark.constraints import DEFAULT_QUOTA_FILE, DEFAULT_SLO_FILE, current_account_id  # noqa: E402
from bedrock_benchmark.models import DEFAULT_MODELS_FILE, load_models  # noqa: E402
from bedrock_benchmark.pilot import format_check, run_pilot_sync  # noqa: E402
from bedrock_benchmark.workload import DEFAULT_WORKLOADS_FILE  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("experiments", nargs="*", help="experiment YAMLs (default: experiments/*.yaml)")
    parser.add_argument("--models-file", default=DEFAULT_MODELS_FILE, help=f"default: {DEFAULT_MODELS_FILE}")
    parser.add_argument("--workloads-file", default=DEFAULT_WORKLOADS_FILE,
                        help=f"workload catalog (default: {DEFAULT_WORKLOADS_FILE})")
    parser.add_argument("--slo-file", default=DEFAULT_SLO_FILE, help=f"SLO profiles (default: {DEFAULT_SLO_FILE})")
    parser.add_argument("--quota-file", default=DEFAULT_QUOTA_FILE, help=f"per-model quotas (default: {DEFAULT_QUOTA_FILE})")
    parser.add_argument("--account", help="AWS account whose quotas apply (default: the live account from STS)")
    parser.add_argument("--slo-profile", action="append", dest="slo_profiles", metavar="NAME",
                        help="run only workloads bound to this SLO profile, e.g. gold (repeatable)")
    parser.add_argument("--model", action="append", dest="models", metavar="NAME",
                        help="run only this model (repeatable; default: every enabled model)")
    parser.add_argument("--results-dir", help="batch output dir (default: results/run-all-<timestamp>)")
    parser.add_argument("--dry-run", action="store_true", help="validate + print the plan and time estimate, run nothing")
    parser.add_argument("--fail-fast", action="store_true", help="stop at the first failed run")
    parser.add_argument("--gateway-config", help="gateway limits snapshot; runs gateway_diff over all produced profiles")
    parser.add_argument("--pilot", action="store_true",
                        help="smoke test only: a few sequential requests per model x workload the plan would use "
                             "(access, workload shape, SLO reachability), then exit without running the batch")
    parser.add_argument("--pilot-requests", type=int, default=3, metavar="N",
                        help="requests per model x workload in the pilot (default: 3)")
    args = parser.parse_args()

    paths = args.experiments or sorted(str(p) for p in Path("experiments").glob("*.yaml"))
    missing = [p for p in paths if not Path(p).is_file()]
    if missing:
        hint = ""
        if any(m.startswith("#") for m in missing):
            hint = ("\n  (a '#' was passed as an argument -- interactive zsh doesn't treat '#' as a comment "
                    "unless `setopt interactivecomments` is on; drop the trailing comment)")
        parser.error(f"experiment file(s) not found: {missing}{hint}")
    models = load_models(args.models_file, names=args.models, quota_file=args.quota_file,
                         account=args.account or current_account_id())
    if not paths or not models:
        print("no experiments or no enabled models", file=sys.stderr)
        return 1

    print(f"quota account: {models[0].account} ({args.quota_file})")
    print(format_plan(plan(paths, models, slo_file=args.slo_file, workloads_file=args.workloads_file,
                           only_slo_profiles=args.slo_profiles)))
    if args.dry_run:
        return 0

    if args.pilot:
        print(f"\n{'=' * 78}\nPILOT: {args.pilot_requests} sequential requests per model x workload\n{'=' * 78}")
        pilot_report = run_pilot_sync(
            paths, models, requests_per_workload=args.pilot_requests, slo_file=args.slo_file,
            workloads_file=args.workloads_file, only_slo_profiles=args.slo_profiles,
            on_check=lambda c: print(format_check(c), flush=True),
        )
        pilot_dir = Path(f"results/pilot-{time.strftime('%Y%m%d-%H%M%S')}")
        pilot_dir.mkdir(parents=True, exist_ok=True)
        (pilot_dir / "pilot.yaml").write_text(yaml.safe_dump(pilot_report.to_dict(), sort_keys=False, width=120))
        summary = pilot_report.to_dict()["summary"]
        print(f"\npilot: {summary} -> {pilot_dir / 'pilot.yaml'}")
        return pilot_report.exit_code

    gateway_config = None
    if args.gateway_config:
        gateway_config = yaml.safe_load(Path(args.gateway_config).read_text()) or {}

    results_dir = Path(args.results_dir or f"results/run-all-{time.strftime('%Y%m%d-%H%M%S')}")
    batch = run_batch(paths, models, results_dir=results_dir, fail_fast=args.fail_fast, gateway_config=gateway_config,
                      slo_file=args.slo_file, workloads_file=args.workloads_file, only_slo_profiles=args.slo_profiles)

    print(f"\n{'=' * 78}\nSUMMARY\n{'=' * 78}")
    print(format_summary(batch))
    return batch.exit_code


if __name__ == "__main__":
    sys.exit(main())
