"""Runs an ExperimentSpec's full sweep:

1. for each sweep subject -- each workload in isolation, or the one
   WorkloadMix when `mix:` is set -- for each swept concurrency/rate
   value, run `repetitions` measurement windows and compute pooled
   RunMetrics (plus per-class metrics for a mix);
2. one Recommendation per subject (see analysis/capacity.py).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Union

from ..analysis.capacity import (
    INCONCLUSIVE, PASS, Recommendation, SweepAnalysis, SweepPoint, Verdict, analyze_sweep, point_verdict, recommend,
)
from ..analysis.confirmation import (
    ConfirmationPlan, ConfirmationResult, fixed_sequence_confirmed, limits_for, plan_looks, reachable, step,
)
from ..analysis.metrics import DEFAULT_CONFIDENCE, compute_run_metrics
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
    # PASS / FAIL / INCONCLUSIVE per DISCOVERY point, aligned with `points`.
    verdicts: List[Verdict] = field(default_factory=list)
    # Confirmation phase (None/empty when not configured or nothing to confirm).
    confirmation_plan: Optional["ConfirmationPlan"] = None
    confirmations: List["ConfirmationResult"] = field(default_factory=list)


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


def _value(point: SweepPoint) -> float:
    return point.concurrency if point.concurrency is not None else point.rps


def _candidates(points: List[SweepPoint], rec: Recommendation, spec: ExperimentSpec, subject: str,
                how_many: int) -> List[SweepPoint]:
    """The `how_many` highest points in discovery's leading non-failing
    run -- for a rate sweep, only those at or below the provider ceiling:
    production is capped at the ceiling anyway, and a point above it
    passes on burst allowance at best. Returned ascending (the
    fixed-sequence test order)."""
    limit = rec.analysis.stable_pass_max
    eligible = [p for p in points if limit is not None and _value(p) <= limit]
    ceiling = spec.provider_ceilings.get(subject)
    if spec.sweep.type == "rate" and ceiling is not None and ceiling.rps:
        eligible = [p for p in eligible if _value(p) <= ceiling.rps * (1 + 1e-9)]
    eligible.sort(key=_value)
    return eligible[-how_many:]


def _slo_kwargs(slo, *, latency: bool = True) -> dict:
    # Rate gates are always judged three-way at `confidence` (default 95%):
    # observed violation -> FAIL, bound clears -> PASS, else INCONCLUSIVE.
    kwargs = dict(success_rate_min=slo.success_rate_min, throttle_rate_max=slo.throttle_rate_max,
                  confidence=slo.confidence or DEFAULT_CONFIDENCE)
    if latency:
        kwargs.update(ttft_p95_slo_ms=slo.ttft_p95_ms, latency_p95_slo_ms=slo.latency_p95_ms,
                      tpot_p95_slo_ms=slo.tpot_p95_ms)
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
            metric_slo = dict(slo_by_workload={
                n: (c.ttft_p95_ms, c.latency_p95_ms, c.tpot_p95_ms) for n, c in class_slos.items()
            })
            gate_kwargs = _slo_kwargs(blend_slo, latency=False)
            class_gate = {n: _slo_kwargs(c) for n, c in class_slos.items()}
        else:
            blend_slo = spec.slo_for(subject.name)
            metric_slo = dict(ttft_slo_ms=blend_slo.ttft_p95_ms, latency_slo_ms=blend_slo.latency_p95_ms,
                              tpot_slo_ms=blend_slo.tpot_p95_ms)
            gate_kwargs = _slo_kwargs(blend_slo)
            class_gate = None
        confidence = blend_slo.confidence or DEFAULT_CONFIDENCE

        # Per sweep value, per phase: every repetition's results + window.
        # Discovery and confirmation data are kept strictly apart -- see
        # analysis/confirmation.py on why they're never pooled.
        acc: Dict[str, Dict[float, dict]] = {"discovery": {}, "confirmation": {}}

        async def measure(value: float, reps: int, phase: str) -> dict:
            state = acc[phase].setdefault(value, {"results": [], "windows": [], "per_rep": [], "peak": 0})
            offered_rps = value if spec.sweep.type == "rate" else None
            for _ in range(reps):
                rep = len(state["windows"])
                # A distinct seed per repetition -- the same seed would
                # replay one identical arrival pattern R times, which
                # isn't R independent samples. Confirmation seeds are
                # offset so they never replay a discovery pattern.
                seed = None if spec.seed is None else spec.seed + rep + (10_000 if phase == "confirmation" else 0)
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
                state["peak"] = max(state["peak"], target.peak_outstanding)
                window = runner.window
                for r in results:
                    r.tags.update({
                        # The runner already tagged the drawn class;
                        # `subject` differs from it only for a mix.
                        "subject": subject.name, "sweep_type": spec.sweep.type, "sweep_value": value,
                        "repetition": rep, "phase": phase,
                        # Persisted so the JSONL alone is enough to
                        # re-derive every metric with the same window.
                        "window_start": window.start, "window_end": window.end,
                        "measured": window.contains(r.scheduled_at),
                    })
                state["results"].extend(results)
                state["windows"].append(window)
                state["per_rep"].append(compute_run_metrics(
                    results, windows=[window], offered_rps=offered_rps, confidence=confidence, **metric_slo,
                ))
                report.all_results.extend(results)
            return state

        def build(value: float, phase: str, conf: Optional[float] = None) -> SweepPoint:
            """Metrics from ONE phase's data; bounds at `conf` (default:
            the SLO's confidence; confirmation uses the per-look one)."""
            state = acc[phase][value]
            point_conf = conf if conf is not None else confidence
            offered_rps = value if spec.sweep.type == "rate" else None
            class_metrics = {}
            if shares is not None:
                for class_name, share in shares.items():
                    own = [r for r in state["results"] if r.tags.get("workload") == class_name]
                    c = class_slos[class_name]
                    class_metrics[class_name] = compute_run_metrics(
                        own, windows=state["windows"], offered_rps=None if offered_rps is None else offered_rps * share,
                        ttft_slo_ms=c.ttft_p95_ms, latency_slo_ms=c.latency_p95_ms, tpot_slo_ms=c.tpot_p95_ms,
                        confidence=conf if conf is not None else (c.confidence or DEFAULT_CONFIDENCE),
                    )
            return SweepPoint(
                concurrency=int(value) if spec.sweep.type == "concurrency" else None,
                rps=value if spec.sweep.type == "rate" else None,
                metrics=compute_run_metrics(
                    state["results"], windows=state["windows"], offered_rps=offered_rps, confidence=point_conf,
                    **metric_slo,
                ),
                repetitions=state["per_rep"],
                class_metrics=class_metrics,
                peak_outstanding=state["peak"],
                client_limited=state["peak"] > target.executor_workers,
                phase=phase,
            )

        # Phase 1 -- discovery: every value, spec.repetitions each.
        values = spec.sweep_values(subject.name)
        points: List[SweepPoint] = []
        for value in values:
            await measure(value, spec.repetitions, "discovery")
            point = build(value, "discovery")
            points.append(point)
            if on_progress is not None:
                on_progress(subject.name, value, point)

        # Phase 2 -- confirmation (analysis/confirmation.py): fresh,
        # independent repetitions at candidates chosen from discovery,
        # PASS only at pre-planned looks, FAIL any time, caps ->
        # INCONCLUSIVE. Discovery data is not reused here.
        recommendation = recommend(points, class_gate, **gate_kwargs)
        plan = None
        confirmations: List[ConfirmationResult] = []
        if spec.confirmation is not None and recommendation is not None:
            cfg = spec.confirmation
            plan = plan_looks(
                limits_for(gate_kwargs, class_gate, shares), confidence=gate_kwargs["confidence"],
                max_looks=cfg.max_looks, max_repetitions=cfg.max_repetitions,
                max_requests=cfg.max_requests, max_duration_s=cfg.max_duration_s,
            )
            gate_look = {**gate_kwargs, "confidence": plan.per_look_confidence}
            class_look = None if class_gate is None else {
                n: {**kw, "confidence": plan.per_look_confidence} for n, kw in class_gate.items()
            }
            candidates = _candidates(points, recommendation, spec, subject.name, cfg.candidates)
            per_rep_s = spec.warmup_s + spec.duration_s
            started = time.perf_counter()
            stopped = False
            for disc in candidates:  # ascending: fixed-sequence order
                value = _value(disc)
                if stopped:
                    confirmations.append(ConfirmationResult(value, INCONCLUSIVE, "not_tested"))
                    continue
                est_per_rep = disc.metrics.n / max(1, len(disc.repetitions))
                result = None
                looks_used = 0
                if not reachable(plan, est_requests_per_rep=est_per_rep, per_rep_s=per_rep_s,
                                 remaining_duration_s=cfg.max_duration_s - (time.perf_counter() - started)):
                    result = ConfirmationResult(value, INCONCLUSIVE, "unreachable_within_caps",
                                                next_look_n=plan.look_schedule[0])
                while result is None:
                    state = await measure(value, 1, "confirmation")
                    point = build(value, "confirmation", conf=plan.per_look_confidence)
                    if on_progress is not None:
                        on_progress(subject.name, value, point)
                    verdict = point_verdict(point, class_look, **gate_look)
                    n, reps = point.metrics.n, len(state["windows"])
                    decision = step(verdict, n, looks_used, plan)
                    if decision is not None:
                        v, reason, looks_used = decision
                    else:
                        v, reason = INCONCLUSIVE, None
                        remaining = cfg.max_duration_s - (time.perf_counter() - started)
                        if reps >= cfg.max_repetitions:
                            reason = "max_repetitions"
                        elif n >= cfg.max_requests:
                            reason = "max_requests"
                        elif remaining < per_rep_s:
                            reason = "max_duration"
                        else:
                            # Requests still collectable within the caps,
                            # at this candidate's observed rate per rep.
                            more_reps = min(cfg.max_repetitions - reps, int(remaining // per_rep_s))
                            max_n = min(cfg.max_requests, n + (n / reps) * more_reps)
                            if max_n < plan.look_schedule[looks_used]:
                                reason = "unreachable_within_caps"
                    if reason is not None:
                        result = ConfirmationResult(
                            value, v, reason, repetitions=reps, n=n, looks_used=looks_used,
                            next_look_n=plan.look_schedule[looks_used] if looks_used < plan.max_looks else None,
                            detail=verdict, point=point,
                        )
                confirmations.append(result)
                stopped = result.verdict != PASS
            confirmed = fixed_sequence_confirmed(confirmations)
            recommendation.confirmed_point = confirmed.point if confirmed is not None else None
            recommendation.confirmation_source = "confirmation"

        report.profiles.append(ProfileReport(
            workload_name=subject.name, points=points, mix_shares=shares,
            recommendation=recommendation,
            analysis=analyze_sweep(points, class_gate, **gate_kwargs),
            confirmation_plan=plan,
            confirmations=confirmations,
            verdicts=[point_verdict(p, class_gate, **gate_kwargs) for p in points],
        ))

    return report
