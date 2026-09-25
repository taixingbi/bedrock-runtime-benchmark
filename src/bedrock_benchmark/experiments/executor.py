"""Runs an ExperimentSpec's full sweep:

1. for each sweep subject -- each workload in isolation, or the one
   WorkloadMix when `mix:` is set -- for each swept concurrency/rate
   value, run `repetitions` measurement windows and compute pooled
   RunMetrics (plus per-class metrics for a mix);
2. one Recommendation per subject (see analysis/capacity.py).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Union

from ..analysis.capacity import Recommendation, SweepPoint, recommend
from ..analysis.metrics import DEFAULT_CONFIDENCE, MeasurementWindow, compute_run_metrics
from ..client import BedrockConverseTarget
from ..results import RequestResult
from ..runners.concurrency import ConcurrencyRunner
from ..runners.rate import RateRunner
from ..workload import WorkloadMix, WorkloadProfile
from .schema import ExperimentSpec

# (subject_name, sweep_value, point) -- called once per completed
# sweep point, so a long multi-point sweep isn't silent until it's
# entirely done.
ProgressCallback = Callable[[str, float, SweepPoint], None]


@dataclass
class ProfileReport:
    # A workload name, or the mix name for a mixed-workload sweep.
    workload_name: str
    points: List[SweepPoint] = field(default_factory=list)
    recommendation: Optional[Recommendation] = None
    # Normalized class shares when this subject is a WorkloadMix.
    mix_shares: Optional[Dict[str, float]] = None


@dataclass
class ExperimentReport:
    spec: ExperimentSpec
    profiles: List[ProfileReport] = field(default_factory=list)
    all_results: List[RequestResult] = field(default_factory=list)


async def run_experiment(
    spec: ExperimentSpec, *, on_progress: Optional[ProgressCallback] = None,
    target: Optional[BedrockConverseTarget] = None,
) -> ExperimentReport:
    # `target` is injectable for tests (a fake client) -- never set by the CLI.
    if target is None:
        target = BedrockConverseTarget(
            model_id=spec.target.model_id, region=spec.target.region, transport=spec.transport,
        )
    report = ExperimentReport(spec=spec)
    if spec.sweep.type not in ("concurrency", "rate"):
        raise ValueError(f"unknown sweep type: {spec.sweep.type!r} (use 'concurrency' or 'rate')")

    profiles = {w.name: w for w in spec.workloads}

    subjects: List[Union[WorkloadProfile, WorkloadMix]]
    if spec.mix is not None:
        subjects = [WorkloadMix(
            name=spec.mix.name, entries=[(profiles[n], w) for n, w in spec.mix.weights.items()],
        )]
    else:
        subjects = [profiles[w.name] for w in spec.workloads]

    confidence = spec.slo.confidence or DEFAULT_CONFIDENCE
    metric_kwargs = dict(
        ttft_slo_ms=spec.slo.ttft_p95_ms, latency_slo_ms=spec.slo.latency_p95_ms, confidence=confidence,
    )

    for subject in subjects:
        shares = subject.shares if isinstance(subject, WorkloadMix) else None
        points: List[SweepPoint] = []
        for value in spec.sweep.values:
            offered_rps = value if spec.sweep.type == "rate" else None

            point_results: List[RequestResult] = []
            windows: List[MeasurementWindow] = []
            per_rep = []
            for rep in range(spec.repetitions):
                # A distinct seed per repetition -- the same seed would
                # replay one identical arrival pattern R times, which
                # isn't R independent samples.
                seed = None if spec.seed is None else spec.seed + rep
                if spec.sweep.type == "concurrency":
                    runner = ConcurrencyRunner(
                        target, subject, concurrency=int(value), duration_s=spec.duration_s,
                        warmup_s=spec.warmup_s, stream=spec.stream, seed=seed,
                    )
                else:
                    runner = RateRunner(
                        target, subject, rps=value, duration_s=spec.duration_s, warmup_s=spec.warmup_s,
                        stream=spec.stream, seed=seed,
                    )

                results = await runner.run()
                window = runner.window
                for r in results:
                    r.tags.update({
                        # The runner already tagged the drawn class;
                        # `subject` differs from it only for a mix.
                        "subject": subject.name, "sweep_type": spec.sweep.type, "sweep_value": value,
                        "repetition": rep,
                        # Persisted so the JSONL alone is enough to
                        # re-derive every metric with the same window.
                        "window_start": window.start, "window_end": window.end,
                        "measured": window.contains(r.scheduled_at),
                    })
                point_results.extend(results)
                windows.append(window)
                per_rep.append(compute_run_metrics(results, windows=[window], offered_rps=offered_rps, **metric_kwargs))

            report.all_results.extend(point_results)
            class_metrics = {}
            if shares is not None:
                for class_name, share in shares.items():
                    own = [r for r in point_results if r.tags.get("workload") == class_name]
                    class_metrics[class_name] = compute_run_metrics(
                        own, windows=windows,
                        offered_rps=None if offered_rps is None else offered_rps * share, **metric_kwargs,
                    )
            point = SweepPoint(
                concurrency=int(value) if spec.sweep.type == "concurrency" else None,
                rps=value if spec.sweep.type == "rate" else None,
                metrics=compute_run_metrics(point_results, windows=windows, offered_rps=offered_rps, **metric_kwargs),
                repetitions=per_rep,
                class_metrics=class_metrics,
            )
            points.append(point)
            if on_progress is not None:
                on_progress(subject.name, value, point)

        recommendation = recommend(
            points, success_rate_min=spec.slo.success_rate_min, throttle_rate_max=spec.slo.throttle_rate_max,
            ttft_p95_slo_ms=spec.slo.ttft_p95_ms, latency_p95_slo_ms=spec.slo.latency_p95_ms,
            gate_on_bounds=spec.slo.confidence is not None,
        )
        report.profiles.append(ProfileReport(
            workload_name=subject.name, points=points, recommendation=recommendation, mix_shares=shares,
        ))

    return report
