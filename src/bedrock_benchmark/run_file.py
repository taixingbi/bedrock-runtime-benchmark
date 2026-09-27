"""Run one experiment YAML against one model end to end -- bind, sweep,
print progress and recommendations, write the raw JSONL +
capacity-profile.yaml into <results_dir>/<model name>/. Shared by
scripts/run.py and scripts/run_all.py, so both produce identical
output and artifacts.
"""
from __future__ import annotations

import asyncio
import math
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Collection, List, Optional

import yaml

from .analysis.capacity import SweepPoint
from .analysis.metrics import DEFAULT_CONFIDENCE, min_samples_to_resolve_rate
from .client import BedrockConverseTarget
from .experiments.executor import ExperimentReport, _slo_kwargs, run_experiment
from .constraints import DEFAULT_SLO_FILE
from .experiments.schema import ExperimentSpec, load_experiment
from .models import ModelConfig
from .workload import DEFAULT_WORKLOADS_FILE
from .recommendation import admission_envelope
from .report import build_capacity_profile
from .storage import write_jsonl

# Builds the Bedrock target for a spec -- injectable so tests can
# substitute a fake client; None means the real boto3 target.
TargetFactory = Callable[[ExperimentSpec], BedrockConverseTarget]


@dataclass
class RunOutcome:
    spec: ExperimentSpec
    report: ExperimentReport
    capacity_profile: dict
    jsonl_path: Path
    profile_path: Path
    elapsed_s: float


def estimated_duration_s(spec: ExperimentSpec) -> float:
    """Wall-time estimate: every sweep point runs warmup + window per
    repetition, per sweep subject (each workload, or one mix), plus --
    with confirmation -- per subject:

    the repetitions needed to reach the FIRST pre-planned look at each
    likely candidate, i.e. assuming it PASSes there -- 0 if that look is
    unreachable within the caps (skipped unspent). A rate sweep's
    candidates are its highest rates at or below the provider ceiling;
    a concurrency sweep's request rate isn't known up front, so its
    candidates are assumed to run at the ceiling (a non-failing point
    can't sustain much more). Capped at min(max_duration_s, candidates
    x max_repetitions x (warmup + window)); without a known ceiling,
    the cap itself. Plus `cooldown_s` once per subject that confirms.

    Real runs take longer when a look is spent on a stray bad event
    (up to the caps) and shorter on an early FAIL. Drain time on top
    depends on real latency, so it isn't counted."""
    per_run = spec.warmup_s + spec.duration_s
    discovery = spec.sweep.point_count * spec.repetitions * per_run
    total = 0.0
    for subject in spec.subject_names:
        confirm = _confirmation_estimate_s(spec, subject, per_run)
        total += discovery + confirm + (spec.confirmation.cooldown_s if confirm > 0 else 0.0)
    return total


# Quota-relative sweep values are rounded to 4 decimals (schema.sweep_values),
# so the 1.0x point (e.g. 6.6667 rps) sits a hair above the unrounded
# ceiling (6.66666...). Without this tolerance the 1.0x point -- the most
# useful candidate -- would never be confirmed.
_ROUNDING_TOLERANCE = 1e-4


def _confirmation_estimate_s(spec: ExperimentSpec, subject: str, per_run: float) -> float:
    c = spec.confirmation
    if c is None:
        return 0.0
    cap = min(c.max_duration_s, c.candidates * c.max_repetitions * per_run)
    ceiling = spec.provider_ceilings.get(subject)
    if ceiling is None or not ceiling.rps:
        return cap
    from .analysis.confirmation import limits_for, plan_looks
    if spec.mix is not None:
        total_weight = sum(spec.mix.weights.values())
        shares = {n: w / total_weight for n, w in spec.mix.weights.items()}
        gate = _slo_kwargs(spec.slo, latency=False)
        class_gate = {n: _slo_kwargs(spec.slo_for(n)) for n in shares}
    else:
        shares, class_gate = None, None
        gate = _slo_kwargs(spec.slo_for(subject))
    plan = plan_looks(limits_for(gate, class_gate, shares), confidence=gate["confidence"],
                      max_looks=c.max_looks, max_repetitions=c.max_repetitions, max_requests=c.max_requests,
                      max_duration_s=c.max_duration_s)
    if spec.sweep.type == "rate":
        rates = sorted(v for v in spec.sweep_values(subject) if v <= ceiling.rps + _ROUNDING_TOLERANCE)[-c.candidates:]
    else:
        rates = [ceiling.rps] * min(c.candidates, len(spec.sweep.values))
    total = 0.0
    for rps in rates:
        per_rep = rps * spec.duration_s
        reps = math.ceil(plan.look_schedule[0] / per_rep) if per_rep > 0 else c.max_repetitions + 1
        if reps <= c.max_repetitions and plan.look_schedule[0] <= c.max_requests:
            total += reps * per_run
    return min(total, cap)


def describe_sweep(spec: ExperimentSpec) -> str:
    """e.g. "concurrency [1, 2, 4]" or "rate 0.25x-2.5x of ceiling:
    short=6.67rps(rpm)" -- a quota-relative sweep's rps differ per
    subject, so the ceiling each resolves against is shown."""
    if spec.sweep.quota_fractions is None:
        stop = f" until {spec.sweep.stop_after_fails} FAILs" if spec.sweep.stop_after_fails else ""
        return f"{spec.sweep.type} {spec.sweep.values}{stop}"
    f = spec.sweep.quota_fractions
    ceilings = ", ".join(
        f"{name}={c.rps:.4g}rps({c.binding})" for name, c in spec.provider_ceilings.items()
    )
    return f"rate {min(f):g}x-{max(f):g}x of ceiling: {ceilings}"


def recommendation_summary(report: ExperimentReport) -> List[str]:
    """One line per sweep subject -- used both after a single run and in
    run_all's final table."""
    lines = []
    for profile_report in report.profiles:
        rec = profile_report.recommendation
        if rec is None:
            lines.append(f"{profile_report.workload_name}: NO swept value met the configured SLO")
            continue
        value = rec.point.concurrency if rec.point.concurrency is not None else rec.point.rps
        sat = None
        if rec.saturation_point is not None:
            sat = rec.saturation_point.concurrency if rec.saturation_point.concurrency is not None else rec.saturation_point.rps
        confirmed = rec.confirmed_point
        confirmed_value = None if confirmed is None else (
            confirmed.concurrency if confirmed.concurrency is not None else confirmed.rps)
        spec = report.spec
        if spec.purpose != "reference":
            policy = "no recommendation (characterization experiment)"
        elif confirmed_value is None:
            policy = "no recommendation (nothing statistically confirmed)"
        else:
            ceiling = spec.provider_ceilings.get(profile_report.workload_name)
            envelope = admission_envelope(
                spec.sweep.type, confirmed_value, headroom=spec.provider_headroom,
                quota_headroom=spec.quota_headroom, provider_ceiling_rps=ceiling.rps if ceiling else None,
            )["admission_envelope"]
            policy = "no recommendation (confirmed point too small for the headroom)" if envelope is None else (
                f"recommendation: max_inflight={envelope['max_inflight']}" if spec.sweep.type == "concurrency"
                else f"recommendation: sustained_rps={envelope['sustained_rps']}")
        lines.append(
            f"{profile_report.workload_name}: measured observed_nonfailing={value} [{rec.verdict.verdict}] "
            f"statistically_confirmed={confirmed_value} saturation={sat} | {policy}"
        )
    return lines


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
            f"ttft_p95={ttft:<10} tpot_p95={m.tpot_p95_ms}ms latency_p95={m.latency_p95_ms}ms slo_goodput={goodput}"
        )

    return printer


def _warn_if_throttle_slo_unresolvable(spec: ExperimentSpec) -> None:
    """Rate sweeps know their expected sample size up front -- say so
    before spending real Bedrock calls if the throttle SLO can't be
    statistically demonstrated at that size."""
    confidence = spec.slo.confidence or DEFAULT_CONFIDENCE
    needed = min_samples_to_resolve_rate(spec.slo.throttle_rate_max, confidence=confidence)
    if spec.sweep.type != "rate":
        print(f"note: resolving throttle_rate_max={spec.slo.throttle_rate_max} at {confidence:.0%} "
              f"needs >= {needed} measured requests per point")
        return
    short = sorted({
        v for name in spec.subject_names for v in spec.sweep_values(name)
        if v * spec.duration_s * spec.repetitions < needed
    })
    if short:
        gate = "these points will FAIL the SLO gate" if spec.slo.confidence is not None else \
            "a 0-throttle pass at these points is not statistically meaningful"
        print(f"warning: rates {short} expect fewer than {needed} measured requests "
              f"(duration_s x repetitions too short to resolve throttle_rate_max="
              f"{spec.slo.throttle_rate_max} at {confidence:.0%}) -- {gate}")


def run_file(
    path: str, model: ModelConfig, *, results_dir: str = "results", target_factory: Optional[TargetFactory] = None,
    slo_file: str = DEFAULT_SLO_FILE, workloads_file: str = DEFAULT_WORKLOADS_FILE,
    only_slo_profiles: Optional[Collection[str]] = None,
) -> RunOutcome:
    spec = load_experiment(path, model, slo_file=slo_file, workloads_file=workloads_file,
                           only_slo_profiles=only_slo_profiles)
    print(f"running experiment: {spec.name} on {model.name} ({model.model_id})")
    print(f"sweep: {describe_sweep(spec)}")
    for name in spec.subject_names:
        if spec.sweep.quota_fractions is not None:
            print(f"  {name}: {spec.sweep_values(name)} rps")
    if spec.description:
        print(spec.description.strip())
    _warn_if_throttle_slo_unresolvable(spec)

    start = time.perf_counter()
    target = target_factory(spec) if target_factory is not None else None
    report = asyncio.run(run_experiment(spec, on_progress=_make_progress_printer(), target=target))
    elapsed_s = time.perf_counter() - start

    print("\n-- input-token calibration --")
    for name, c in report.calibrations.items():
        if c.method == "estimate":
            print(f"  {name}: estimate, 4 chars/token ({c.note})")
        else:
            status = "converged" if c.converged else "closest, not within tolerance"
            print(f"  {name}: {c.method} -> {c.counted_input_tokens} tokens ({status}, {c.iterations} steps)"
                  + (f" [{c.note}]" if c.note else ""))

    print("\n-- recommendations --")
    for line in recommendation_summary(report):
        print(f"  {line}")
    for profile_report in report.profiles:
        rec = profile_report.recommendation
        if rec is None:
            continue
        for class_name, m in rec.point.class_metrics.items():
            print(f"    {class_name}: n={m.n} ttft_p95={m.ttft_p95_ms}ms tpot_p95={m.tpot_p95_ms}ms "
                  f"latency_p95={m.latency_p95_ms}ms "
                  f"slo_goodput={m.slo_goodput_rps}")

    run_id = str(uuid.uuid4())[:8]
    out_dir = Path(results_dir) / model.name
    jsonl_path = out_dir / f"{spec.name}-{run_id}.jsonl"
    profile_path = out_dir / f"{spec.name}-{run_id}-capacity-profile.yaml"

    write_jsonl(report.all_results, str(jsonl_path))
    capacity_profile = build_capacity_profile(report)
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_text(yaml.safe_dump(capacity_profile, sort_keys=False))

    for name, entry in capacity_profile["workload_classes"].items():
        v = entry["workload_validation"]
        for side, why in (("input", f"padding sized by {v['token_counting']['method']} missed"),
                          ("output", "the model stopped well short of max_tokens")):
            c = v[side]
            if c["valid"] is False:
                print(f"\nwarning: {name} {side} p50 {c['observed_p50']} tokens vs target {c['target']} "
                      f"({c['deviation_pct']}%, tolerance {c['tolerance_pct']}%) -- {why}; "
                      f"its envelope describes a different workload shape")
    for subject, entry in {**capacity_profile["workload_classes"], **capacity_profile.get("mixed_workloads", {})}.items():
        if entry.get("client_limited_points"):
            print(f"\nwarning: {subject} points {entry['client_limited_points']} queued for client threads "
                  f"(peak outstanding > executor_workers={capacity_profile['transport']['executor_workers']}) "
                  f"-- excluded from the recommendation; raise transport.max_connections")

    print(f"\nraw results:      {jsonl_path}")
    print(f"capacity profile: {profile_path}")
    return RunOutcome(
        spec=spec, report=report, capacity_profile=capacity_profile,
        jsonl_path=jsonl_path, profile_path=profile_path, elapsed_s=elapsed_s,
    )
