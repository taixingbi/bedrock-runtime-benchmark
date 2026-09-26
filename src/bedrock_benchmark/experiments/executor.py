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

from ..analysis.capacity import Recommendation, SweepAnalysis, SweepPoint, analyze_sweep, recommend
from ..analysis.metrics import DEFAULT_CONFIDENCE, MeasurementWindow, compute_run_metrics
from ..calibration import CalibrationResult, calibrate_profile, estimate_profile, resolve_counter
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
    # Always set -- also when there's no recommendation, so the report
    # can say WHY (never passed vs non-monotonic from the first point).
    analysis: Optional[SweepAnalysis] = None


@dataclass
class ExperimentReport:
    spec: ExperimentSpec
    profiles: List[ProfileReport] = field(default_factory=list)
    all_results: List[RequestResult] = field(default_factory=list)
    calibrations: Dict[str, CalibrationResult] = field(default_factory=dict)


def calibrate_workloads(spec: ExperimentSpec, target: BedrockConverseTarget) -> Dict[str, CalibrationResult]:
    """Resolve the model's token counter once (CountTokens > Converse
    usage > estimate), then size every workload's padding with it."""
    method, count_fn, notes = resolve_counter(spec.token_counting, [
        ("count_tokens", target.count_tokens),
        ("converse_usage", target.usage_input_tokens),
    ])
    if method is None:
        note = "; ".join(notes) or "no provider token counter available"
        return {w.name: estimate_profile(w, note) for w in spec.workloads}
    out = {}
    for w in spec.workloads:
        result = calibrate_profile(w, count_fn, method, tolerance_pct=spec.calibration_tolerance_pct)
        if notes and not result.note:
            result.note = "; ".join(notes)
        out[w.name] = result
    return out


def _slo_kwargs(slo, *, latency: bool = True) -> dict:
    kwargs = dict(success_rate_min=slo.success_rate_min, throttle_rate_max=slo.throttle_rate_max,
                  gate_on_bounds=slo.confidence is not None)
    if latency:
        kwargs.update(ttft_p95_slo_ms=slo.ttft_p95_ms, latency_p95_slo_ms=slo.latency_p95_ms)
    return kwargs


async def run_experiment(
    spec: ExperimentSpec, *, on_progress: Optional[ProgressCallback] = None,
    target: Optional[BedrockConverseTarget] = None,
) -> ExperimentReport:
    # `target` is injectable for tests (a fake client) -- never set by the CLI.
    owns_target = target is None
    if target is None:
        target = BedrockConverseTarget(
            model_id=spec.target.model_id, region=spec.target.region, transport=spec.transport,
        )
    try:
        return await _run(spec, target, on_progress)
    finally:
        if owns_target:
            target.close()


async def _run(spec: ExperimentSpec, target: BedrockConverseTarget, on_progress: Optional[ProgressCallback]) -> ExperimentReport:
    report = ExperimentReport(spec=spec)
    if spec.sweep.type not in ("concurrency", "rate"):
        raise ValueError(f"unknown sweep type: {spec.sweep.type!r} (use 'concurrency' or 'rate')")

    report.calibrations = calibrate_workloads(spec, target)
    profiles = {name: c.profile for name, c in report.calibrations.items()}
    subjects: List[Union[WorkloadProfile, WorkloadMix]]
    if spec.mix is not None:
        subjects = [WorkloadMix(
            name=spec.mix.name, entries=[(profiles[n], w) for n, w in spec.mix.weights.items()],
        )]
    else:
        subjects = [profiles[w.name] for w in spec.workloads]

    for subject in subjects:
        is_mix = isinstance(subject, WorkloadMix)
        shares = subject.shares if is_mix else None
        # SLOs: an isolated workload uses its own (profile or default).
        # A mix judges each class against ITS SLO -- per-request for
        # goodput, per-class for the gate -- and the blend only on the
        # rate gates (success/throttle) of the default SLO.
        if is_mix:
            class_slos = {n: spec.slo_for(n) for n in shares}
            blend_slo = spec.slo
            metric_slo = dict(slo_by_workload={n: (c.ttft_p95_ms, c.latency_p95_ms) for n, c in class_slos.items()})
            gate_kwargs = _slo_kwargs(blend_slo, latency=False)
            class_gate = {n: _slo_kwargs(c) for n, c in class_slos.items()}
        else:
            blend_slo = spec.slo_for(subject.name)
            metric_slo = dict(ttft_slo_ms=blend_slo.ttft_p95_ms, latency_slo_ms=blend_slo.latency_p95_ms)
            gate_kwargs = _slo_kwargs(blend_slo)
            class_gate = None
        confidence = blend_slo.confidence or DEFAULT_CONFIDENCE

        points: List[SweepPoint] = []
        for value in spec.sweep_values(subject.name):
            offered_rps = value if spec.sweep.type == "rate" else None

            point_results: List[RequestResult] = []
            windows: List[MeasurementWindow] = []
            per_rep = []
            peak = 0
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

                target.reset_peak()
                results = await runner.run()
                peak = max(peak, target.peak_outstanding)
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
                per_rep.append(compute_run_metrics(
                    results, windows=[window], offered_rps=offered_rps, confidence=confidence, **metric_slo,
                ))

            report.all_results.extend(point_results)
            class_metrics = {}
            if shares is not None:
                for class_name, share in shares.items():
                    own = [r for r in point_results if r.tags.get("workload") == class_name]
                    c = class_slos[class_name]
                    class_metrics[class_name] = compute_run_metrics(
                        own, windows=windows, offered_rps=None if offered_rps is None else offered_rps * share,
                        ttft_slo_ms=c.ttft_p95_ms, latency_slo_ms=c.latency_p95_ms,
                        confidence=c.confidence or DEFAULT_CONFIDENCE,
                    )
            point = SweepPoint(
                concurrency=int(value) if spec.sweep.type == "concurrency" else None,
                rps=value if spec.sweep.type == "rate" else None,
                metrics=compute_run_metrics(
                    point_results, windows=windows, offered_rps=offered_rps, confidence=confidence, **metric_slo,
                ),
                repetitions=per_rep,
                class_metrics=class_metrics,
                peak_outstanding=peak,
                client_limited=peak > target.executor_workers,
            )
            points.append(point)
            if on_progress is not None:
                on_progress(subject.name, value, point)

        report.profiles.append(ProfileReport(
            workload_name=subject.name, points=points, mix_shares=shares,
            recommendation=recommend(points, class_gate, **gate_kwargs),
            analysis=analyze_sweep(points, class_gate, **gate_kwargs),
        ))

    return report
