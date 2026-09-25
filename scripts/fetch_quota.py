#!/usr/bin/env python3
"""CLI: python scripts/fetch_quota.py --model-id <id> [--region ...]

Prints a model's real RPM/TPM quota (table-first, AWS Service Quotas
fallback -- see quota.py's own docstring) as a YAML snippet ready to
paste into an experiment's `quota_snapshot:` block. Deliberately a
manual, explicit step, not something run_experiment() calls
automatically -- matches this repo's existing style (see
bedrock-runtime-gateway's own sync_model_quotas_from_aws.py, which is
equally explicit/manual for the same reason: a number an experiment's
sweep values get DESIGNED around shouldn't silently change between one
run and the next just because a background lookup returned something
different this time).

Usage:
    python scripts/fetch_quota.py --model-id us.amazon.nova-pro-v1:0
    python scripts/fetch_quota.py --model-id us.amazon.nova-pro-v1:0 --region us-west-2
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bedrock_benchmark.quota import fetch_quota_snapshot  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--table-name", default="gateway-model-quotas-dev")
    args = parser.parse_args()

    quota = fetch_quota_snapshot(args.model_id, region=args.region, table_name=args.table_name)

    if quota.source == "unknown":
        print(f"could not determine quota for {args.model_id!r} (table unreachable, and no Service Quotas mapping/permission)", file=sys.stderr)
        raise SystemExit(1)

    rps = round(quota.rpm / 60.0, 2) if quota.rpm is not None else None
    print(f"# {args.model_id} ({args.region}) -- source: {quota.source}")
    print(f"# {quota.rpm:.0f} RPM ≈ {rps} rps" if quota.rpm is not None else "# rpm: unknown")
    print("quota_snapshot:")
    print(f"  rpm: {int(quota.rpm) if quota.rpm is not None else 'null'}")
    print(f"  tpm: {int(quota.tpm) if quota.tpm is not None else 'null'}")


if __name__ == "__main__":
    main()
