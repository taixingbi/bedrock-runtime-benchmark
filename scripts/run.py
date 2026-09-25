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
from bedrock_benchmark.analysis.metrics import DEFAULT_CONFIDENCE, min_samples_to_resolve_rate  # noqa: E402
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
            f"n={m.n:<5} success={m.success_rate:.3f} throttle={m.throttle_rate:.4f} "
            f"(<={m.throttle_rate_upper}) "
            f"ttft_p95={ttft:<10} latency_p95={m.latency_p95_ms}ms slo_goodput={goodput}"
        )

    return printer


def _warn_if_throttle_slo_unresolvable(spec) -> None:
    """Rate sweeps know their expected sample size up front -- say so
    before spending real Bedrock calls if the throttle SLO can't be
    statistically demonstrated at that size."""
    confidence = spec.slo.confidence or DEFAULT_CONFIDENCE
    needed = min_samples_to_resolve_rate(spec.slo.throttle_rate_max, confidence=confidence)
    if spec.sweep.type != "rate":
        print(f"note: resolving throttle_rate_max={spec.slo.throttle_rate_max} at {confidence:.0%} "
              f"needs >= {needed} measured requests per point")
        return
    short = [v for v in spec.sweep.values if v * spec.duration_s * spec.repetitions < needed]
    if short:
        gate = "these points will FAIL the SLO gate" if spec.slo.confidence is not None else \
            "a 0-throttle pass at these points is not statistically meaningful"
        print(f"warning: rates {short} expect fewer than {needed} measured requests "
              f"(duration_s x repetitions too short to resolve throttle_rate_max="
              f"{spec.slo.throttle_rate_max} at {confidence:.0%}) -- {gate}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("experiment", help="path to an experiment YAML file")
    parser.add_argument("--results-dir", default="results", help="where to write JSONL + capacity-profile.yaml")
    args = parser.parse_args()

    spec = load_experiment(args.experiment)
    print(f"running experiment: {spec.name} (sweep={spec.sweep.type} values={spec.sweep.values})")
    if spec.description:
        print(spec.description.strip())

    _warn_if_throttle_slo_unresolvable(spec)

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
        for class_name, m in rec.point.class_metrics.items():
            print(f"    {class_name}: n={m.n} latency_p95={m.latency_p95_ms}ms ttft_p95={m.ttft_p95_ms}ms "
                  f"slo_goodput={m.slo_goodput_rps}")

    run_id = str(uuid.uuid4())[:8]
    results_dir = Path(args.results_dir)
    jsonl_path = results_dir / f"{spec.name}-{run_id}.jsonl"
    profile_path = results_dir / f"{spec.name}-{run_id}-capacity-profile.yaml"

    write_jsonl(report.all_results, str(jsonl_path))
    capacity_profile = build_capacity_profile(report)
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_text(yaml.safe_dump(capacity_profile, sort_keys=False))

    for name, entry in capacity_profile["workload_classes"].items():
        v = entry["workload_validation"]
        if v["valid"] is False:
            print(f"\nwarning: {name} measured input p50 {v['observed_input_tokens_p50']} tokens vs "
                  f"{v['requested_input_tokens']} requested ({v['deviation_pct']}%, tolerance "
                  f"{v['tolerance_pct']}%) -- the 4-chars/token padding estimate missed for this model; "
                  f"its envelope describes a different workload shape")

    print(f"\nraw results:      {jsonl_path}")
    print(f"capacity profile: {profile_path}")


if __name__ == "__main__":
    main()
