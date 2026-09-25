#!/usr/bin/env python3
"""Compare capacity-profile.yaml artifacts against a snapshot of
bedrock-runtime-gateway's current limits:

    python scripts/gateway_diff.py \\
        --gateway-config examples/gateway-limits.example.yaml \\
        results/*-capacity-profile.yaml

Prints the findings as YAML (warn first). Exits 1 when any warn-level
finding exists, so it can gate a config review in CI. Read-only: it
never modifies gateway config -- see src/bedrock_benchmark/gateway_diff.py.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import yaml  # noqa: E402

from bedrock_benchmark.gateway_diff import diff  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("profiles", nargs="+", help="capacity-profile.yaml files (schema_version 3)")
    parser.add_argument("--gateway-config", required=True, help="YAML snapshot of the gateway's current limits")
    args = parser.parse_args()

    profiles = [yaml.safe_load(Path(p).read_text()) for p in args.profiles]
    gateway = yaml.safe_load(Path(args.gateway_config).read_text()) or {}

    result = diff(profiles, gateway)
    print(yaml.safe_dump(result.to_dict(), sort_keys=False, width=120))
    return 1 if result.to_dict()["summary"]["warn"] else 0


if __name__ == "__main__":
    sys.exit(main())
