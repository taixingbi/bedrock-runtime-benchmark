#!/usr/bin/env python3
"""CLI entrypoint: python scripts/run.py experiments/<name>.yaml

Runs the full sweep, prints a summary table per workload profile (one
row per swept concurrency/rate value) plus the recommendation, writes
the raw per-request JSONL to results/<run-id>.jsonl, and writes the
capacity-profile.yaml artifact to results/<run-id>-capacity-profile.yaml.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import yaml  # noqa: E402

from bedrock_benchmark.analysis.capacity import SweepPoint  # noqa: E402
from bedrock_benchmark.experiments.executor import run_experiment  # noqa: E402
from bedrock_benchmark.experiments.schema import load_experiment  # noqa: E402
from bedrock_benchmark.report import build_capacity_profile  # noqa: E402
from bedrock_benchmark.storage import write_jsonl  # noqa: E402


def _make_progress_printer():
    start = time.perf_counter()

    def printer(workload_name: str, sweep_value: float, point: SweepPoint) -> None:
        elapsed = time.perf_counter() - start
        m = point.metrics
        ttft = f"{m.ttft_p95_ms}ms" if m.ttft_p95_ms is not None else "n/a"
        goodput = f"{m.slo_goodput_rps}" if m.slo_goodput_rps is not None else "n/a"
        print(
            f"  [{elapsed:6.0f}s] {workload_name:<12} value={sweep_value:<6} "
            f"n={m.n:<4} success={m.success_rate:.3f} throttle={m.throttle_rate:.3f} "
            f"ttft_p95={ttft:<10} latency_p95={m.latency_p95_ms}ms slo_goodput={goodput}"
        )

    return printer


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("experiment", help="path to an experiment YAML file")
    parser.add_argument("--results-dir", default="results", help="where to write JSONL + capacity-profile.yaml")
    args = parser.parse_args()

    spec = load_experiment(args.experiment)
    print(f"running experiment: {spec.name} (sweep={spec.sweep.type} values={spec.sweep.values})")
    if spec.description:
        print(spec.description.strip())

    report = asyncio.run(run_experiment(spec, on_progress=_make_progress_printer()))

    print("\n-- recommendations --")
    for profile_report in report.profiles:
        if profile_report.recommendation is None:
            print(f"  {profile_report.workload_name}: NO swept value met the configured SLO")
            continue
        rec = profile_report.recommendation
        value = rec.point.concurrency if rec.point.concurrency is not None else rec.point.rps
        sat = None
        if rec.saturation_point is not None:
            sat = rec.saturation_point.concurrency if rec.saturation_point.concurrency is not None else rec.saturation_point.rps
        print(
            f"  {profile_report.workload_name}: recommended={value} "
            f"slo_goodput_rps={rec.point.metrics.slo_goodput_rps} saturation={sat}"
        )

    run_id = str(uuid.uuid4())[:8]
    results_dir = Path(args.results_dir)
    jsonl_path = results_dir / f"{spec.name}-{run_id}.jsonl"
    profile_path = results_dir / f"{spec.name}-{run_id}-capacity-profile.yaml"

    write_jsonl(report.all_results, str(jsonl_path))
    capacity_profile = build_capacity_profile(report)
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_text(yaml.safe_dump(capacity_profile, sort_keys=False))

    print(f"\nraw results:      {jsonl_path}")
    print(f"capacity profile: {profile_path}")


if __name__ == "__main__":
    main()
