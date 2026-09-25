#!/usr/bin/env python3
"""CLI entrypoint: python scripts/run.py experiments/<name>.yaml

Runs the full sweep, prints a summary table per workload profile (one
row per swept concurrency/rate value) plus the recommendation, writes
the raw per-request JSONL to results/<run-id>.jsonl, and writes the
capacity-profile.yaml artifact to results/<run-id>-capacity-profile.yaml.
To run every experiment in one go, see scripts/run_all.py.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bedrock_benchmark.run_file import run_file  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("experiment", help="path to an experiment YAML file")
    parser.add_argument("--results-dir", default="results", help="where to write JSONL + capacity-profile.yaml")
    args = parser.parse_args()
    run_file(args.experiment, results_dir=args.results_dir)


if __name__ == "__main__":
    main()
