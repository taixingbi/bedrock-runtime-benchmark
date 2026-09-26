#!/usr/bin/env python3
"""CLI: python scripts/fetch_quota.py --all | --model-id <id> [--region ...]

--all checks every model in scripts/models.yaml against its real
RPM/TPM quota and reports which `quota:` entries are stale -- rate
sweeps are quota fractions, so a stale number shifts every rate tested.
--model-id prints one model's quota as a snippet for a new models.yaml
entry. Quotas come table-first, AWS Service Quotas fallback (see
quota.py's own docstring). Deliberately a
manual, explicit step, not something run_experiment() calls
automatically -- matches this repo's existing style (see
bedrock-runtime-gateway's own sync_model_quotas_from_aws.py, which is
equally explicit/manual for the same reason: a number an experiment's
sweep values get DESIGNED around shouldn't silently change between one
run and the next just because a background lookup returned something
different this time).

Usage:
    python scripts/fetch_quota.py --all
    python scripts/fetch_quota.py --model-id us.amazon.nova-pro-v1:0
    python scripts/fetch_quota.py --model-id us.amazon.nova-pro-v1:0 --region us-west-2
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bedrock_benchmark.models import DEFAULT_MODELS_FILE, load_models  # noqa: E402
from bedrock_benchmark.quota import fetch_quota_snapshot  # noqa: E402


def check_all(models_file: str, table_name: str) -> int:
    """Exit 1 if any models-file quota differs from the live value (or
    can't be determined)."""
    stale = 0
    for m in load_models(models_file, include_disabled=True):
        q = fetch_quota_snapshot(m.model_id, region=m.region, table_name=table_name)
        if q.source == "unknown":
            print(f"?  {m.name:<14} {m.model_id}: live quota unknown")
            stale += 1
            continue
        live = (q.rpm, q.tpm)
        filed = (m.quota_rpm, m.quota_tpm)
        if live == filed:
            print(f"ok {m.name:<14} rpm={q.rpm:.0f} tpm={q.tpm:.0f} ({q.source})")
        else:
            stale += 1
            print(f"!! {m.name:<14} models.yaml rpm={filed[0]} tpm={filed[1]} -> live rpm={q.rpm} tpm={q.tpm} ({q.source})")
            print(f"   quota: {{rpm: {int(q.rpm) if q.rpm is not None else 'null'}, "
                  f"tpm: {int(q.tpm) if q.tpm is not None else 'null'}}}")
    return 1 if stale else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--all", action="store_true", help="check every model in the models file (incl. disabled)")
    target.add_argument("--model-id")
    parser.add_argument("--models-file", default=DEFAULT_MODELS_FILE)
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--table-name", default="gateway-model-quotas-dev")
    args = parser.parse_args()

    if args.all:
        raise SystemExit(check_all(args.models_file, args.table_name))

    quota = fetch_quota_snapshot(args.model_id, region=args.region, table_name=args.table_name)

    if quota.source == "unknown":
        print(f"could not determine quota for {args.model_id!r} (table unreachable, and no Service Quotas mapping/permission)", file=sys.stderr)
        raise SystemExit(1)

    rps = round(quota.rpm / 60.0, 2) if quota.rpm is not None else None
    print(f"# {args.model_id} ({args.region}) -- source: {quota.source}")
    print(f"# {quota.rpm:.0f} RPM ≈ {rps} rps" if quota.rpm is not None else "# rpm: unknown")
    print(f"quota: {{rpm: {int(quota.rpm) if quota.rpm is not None else 'null'}, "
          f"tpm: {int(quota.tpm) if quota.tpm is not None else 'null'}}}")


if __name__ == "__main__":
    main()
