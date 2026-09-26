#!/usr/bin/env python3
"""Run ONE experiment against the models in scripts/models.yaml:

    python scripts/run.py experiments/concurrency-sweep.yaml                    # every enabled model
    python scripts/run.py experiments/concurrency-sweep.yaml --model nova-micro # one model

Prints per-point progress and the recommendation, and writes the raw
per-request JSONL + capacity-profile.yaml to results/<model>/. To run
every experiment in one go, see scripts/run_all.py.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bedrock_benchmark.batch import format_summary, run_batch  # noqa: E402
from bedrock_benchmark.constraints import DEFAULT_QUOTA_FILE, DEFAULT_SLO_FILE  # noqa: E402
from bedrock_benchmark.models import DEFAULT_MODELS_FILE, load_models  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("experiment", help="path to an experiment YAML file")
    parser.add_argument("--models-file", default=DEFAULT_MODELS_FILE, help=f"default: {DEFAULT_MODELS_FILE}")
    parser.add_argument("--slo-file", default=DEFAULT_SLO_FILE, help=f"SLO profiles (default: {DEFAULT_SLO_FILE})")
    parser.add_argument("--quota-file", default=DEFAULT_QUOTA_FILE, help=f"per-model quotas (default: {DEFAULT_QUOTA_FILE})")
    parser.add_argument("--model", action="append", dest="models", metavar="NAME",
                        help="run only this model (repeatable; default: every enabled model)")
    parser.add_argument("--results-dir", default="results", help="output root; files go to <results-dir>/<model>/")
    args = parser.parse_args()

    models = load_models(args.models_file, names=args.models, quota_file=args.quota_file)
    batch = run_batch([args.experiment], models, results_dir=Path(args.results_dir), slo_file=args.slo_file)
    if len(batch.results) > 1:
        print(f"\n{format_summary(batch)}")
    return batch.exit_code


if __name__ == "__main__":
    sys.exit(main())
