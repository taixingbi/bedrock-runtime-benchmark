"""Runs an ExperimentSpec's full sweep -- for each workload profile,
for each swept concurrency/rate value, run it for duration_s, compute
RunMetrics, and collect a SweepPoint. One Recommendation per workload
profile at the end (see analysis/capacity.py for exactly how).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from ..client import BedrockConverseTarget
from ..results import RequestResult
from ..runners.concurrency import ConcurrencyRunner
from ..runners.rate import RateRunner
from ..analysis.capacity import Recommendation, SweepPoint, recommend
from ..analysis.metrics import compute_run_metrics
from .schema import ExperimentSpec

# (workload_name, sweep_value, point) -- called once per completed
# sweep point, so a long multi-point sweep isn't silent until it's
# entirely done.
ProgressCallback = Callable[[str, float, SweepPoint], None]


@dataclass
class ProfileReport:
    workload_name: str
    points: List[SweepPoint] = field(default_factory=list)
    recommendation: Optional[Recommendation] = None


@dataclass
class ExperimentReport:
    spec: ExperimentSpec
    profiles: List[ProfileReport] = field(default_factory=list)
    all_results: List[RequestResult] = field(default_factory=list)


async def run_experiment(spec: ExperimentSpec, *, on_progress: Optional[ProgressCallback] = None) -> ExperimentReport:
    target = BedrockConverseTarget(model_id=spec.target.model_id, region=spec.target.region)
    report = ExperimentReport(spec=spec)

    for profile in spec.workloads:
        points: List[SweepPoint] = []
        for value in spec.sweep.values:
            if spec.sweep.type == "concurrency":
                runner = ConcurrencyRunner(
                    target, profile, concurrency=int(value), duration_s=spec.duration_s, stream=spec.stream,
                )
                offered_rps = None
            elif spec.sweep.type == "rate":
                runner = RateRunner(
                    target, profile, rps=value, duration_s=spec.duration_s, stream=spec.stream, seed=spec.seed,
                )
                offered_rps = value
            else:
                raise ValueError(f"unknown sweep type: {spec.sweep.type!r} (use 'concurrency' or 'rate')")

            results = await runner.run()
            for r in results:
                r.tags = {"workload": profile.name, "sweep_type": spec.sweep.type, "sweep_value": value}
            report.all_results.extend(results)

            metrics = compute_run_metrics(
                results, duration_s=spec.duration_s,
                ttft_slo_ms=spec.slo.ttft_p95_ms, latency_slo_ms=spec.slo.latency_p95_ms, offered_rps=offered_rps,
            )
            point = SweepPoint(
                concurrency=int(value) if spec.sweep.type == "concurrency" else None,
                rps=value if spec.sweep.type == "rate" else None,
                metrics=metrics,
            )
            points.append(point)
            if on_progress is not None:
                on_progress(profile.name, value, point)

        recommendation = recommend(
            points, success_rate_min=spec.success_rate_min, throttle_rate_max=spec.throttle_rate_max,
            ttft_p95_slo_ms=spec.slo.ttft_p95_ms, latency_p95_slo_ms=spec.slo.latency_p95_ms,
        )
        report.profiles.append(ProfileReport(workload_name=profile.name, points=points, recommendation=recommendation))

    return report
