"""Builds the capacity-profile.yaml artifact (schema_version 5) -- the
one machine-readable thing this repo exists to hand to
bedrock-runtime-gateway's own control-plane config review, not a
human-facing HTML report.

schema_version 2 fixes two real bugs schema_version 1 had:

1. Rate-sweep results were written into the SAME `saturation_concurrency`
   field a concurrency sweep uses, silently mislabeling an RPS value as
   a concurrency value -- and provider_headroom was only ever applied
   to `.concurrency`, so a rate sweep's recommended RPS was never
   headroom-adjusted at all. Rate and concurrency results now live in
   their own `rate`/`workload_classes.<name>.rate` and
   `.concurrency` sub-blocks with their own headroom-adjusted
   `production_rps`/`production_max`, never sharing a field name.
2. `global_max_concurrency = max(concurrencies)` across independently-
   swept workload classes doesn't mean anything: a real MIXED workload
   (some short traffic + some long traffic concurrently) can exceed
   safe backend capacity well before either class's own isolated
   measured max would predict. There is no such thing as a
   scientifically defensible "global max concurrency" derived from
   per-class isolated sweeps alone -- it needs its own dedicated
   mixed-workload experiment (not yet built). So this artifact reports
   ONLY per-class envelopes now; a gateway's own global concurrency
   config is the gateway's decision to make from these, not something
   this repo pre-packages for it.

schema_version 3 fixes a field-semantics bug schema_version 2 still had:

3. The rate block's `measured_sustainable_rps` was the best point's SLO
   GOODPUT, and `production_rps` applied headroom to that goodput --
   but a gateway admission limit is set on OFFERED load, not on the
   fraction of it that came back within SLO. Those are three different
   numbers now: `max_safe_offered_rps` (the swept rate that passed),
   `slo_goodput_rps` (what it actually delivered within SLO), and
   `production_offered_rps` (headroom applied to the OFFERED rate --
   the one a gateway config should read).

It also records whether each workload class actually measured the
shape it claims (`workload_validation`: requested vs Bedrock-reported
input tokens -- i.e. whether the 4-chars/token padding estimate held
for this model), and -- for a `mix:` experiment -- a `mixed_workloads`
envelope, the only cross-class number this artifact ever reports.

It also records how the numbers were measured (`measurement`: warmup,
window, repetitions, confidence) and per-class `evidence` (sample
size, throttle count, confidence bounds), so a reader can tell a
statistically resolved 0.1% throttle SLO from an unresolved one.

schema_version 4 adds, per sweep subject: `provider_constraints` (RPM-
vs TPM-bound request ceiling, see ceiling.py) and the rps a quota-
relative sweep actually resolved to; `saturation_status` (resolved /
not_reached / unresolved -- a non-monotonic sweep claims no saturation);
input AND output token validation; per-class SLO profiles; and client
integrity evidence (`peak_outstanding`, `client_limited_points`,
`transport.executor_workers`).

schema_version 5 groups the quota snapshot and the SLO profiles under
one `constraints:` block (quota: what the provider allows; slo: what
quality we require), mirroring constraints/quota.yaml and
constraints/slo.yaml -- replacing v4's top-level quota_snapshot/slo/
slo_profiles.

See this repo's own README for the boundary this draws: this repo
outputs a safe operating envelope per workload class; it never
implements or pre-decides a gateway's global/tenant/AIMD control
policy.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from .analysis.capacity import Recommendation, apply_headroom
from .analysis.metrics import DEFAULT_CONFIDENCE, RunMetrics, min_samples_to_resolve_rate, percentile
from .experiments.executor import ExperimentReport
from .results import RequestResult
from .workload import WorkloadProfile


def _observed_tokens(results: List[RequestResult]) -> dict:
    """Real, measured p50 input/output tokens for this workload class
    -- distinct from WorkloadProfile's own input_tokens/output_tokens
    (the TARGET the prompt generator aimed for). Comparing the two is
    the cheapest sanity check that a workload class actually measured
    what it claims to have measured."""
    input_tokens = [r.input_tokens for r in results if r.success and r.input_tokens is not None]
    output_tokens = [r.output_tokens for r in results if r.success and r.output_tokens is not None]
    return {
        "input_tokens_p50": round(percentile(input_tokens, 50), 1) if input_tokens else None,
        "output_tokens_p50": round(percentile(output_tokens, 50), 1) if output_tokens else None,
    }


def _saturation_fields(rec: Recommendation) -> dict:
    """saturation_status is always stated; for a non-monotonic sweep no
    saturation value is claimed -- the unstable region is described
    instead (see analysis/capacity.py's SweepAnalysis)."""
    a = rec.analysis
    out: dict = {"saturation_status": a.status}
    if a.status == "unresolved":
        out.update(unstable_region=a.unstable_region, confirmed_fail_from=a.confirmed_fail_from)
    return out


def _concurrency_block(rec: Recommendation, *, headroom: float) -> dict:
    saturation = rec.saturation_point.concurrency if rec.saturation_point is not None else None
    production_max = max(1, int(apply_headroom(rec.point.concurrency, headroom=headroom)))
    return {
        "measured_best": rec.point.concurrency,
        "saturation": saturation,
        **_saturation_fields(rec),
        "production_max": production_max,
        "slo_goodput_rps": rec.point.metrics.slo_goodput_rps,
    }


def _rate_block(rec: Recommendation, *, headroom: float) -> dict:
    saturation_rps = rec.saturation_point.rps if rec.saturation_point is not None else None
    return {
        "max_safe_offered_rps": rec.point.rps,
        "slo_goodput_rps": rec.point.metrics.slo_goodput_rps,
        "saturation_offered_rps": saturation_rps,
        **_saturation_fields(rec),
        "production_offered_rps": apply_headroom(rec.point.rps, headroom=headroom),
    }


def _evidence(point) -> dict:
    m: RunMetrics = point.metrics
    out = {
        "n": m.n,
        "n_throttled": m.n_throttled,
        "measured_duration_s": m.measured_duration_s,
        "throttle_rate": m.throttle_rate,
        "throttle_rate_upper": m.throttle_rate_upper,
        "success_rate": m.success_rate,
        "success_rate_lower": m.success_rate_lower,
        "bound_confidence": m.bound_confidence,
        "peak_outstanding": point.peak_outstanding,
    }
    if len(point.repetitions) > 1:
        out["repetition_slo_goodput_rps"] = [r.slo_goodput_rps for r in point.repetitions]
    return out


def _deviation(observed: Optional[float], target: int, tolerance_pct: float) -> dict:
    deviation_pct = valid = None
    if observed is not None and target > 0:
        deviation_pct = round((observed - target) / target * 100, 2)
        valid = abs(deviation_pct) <= tolerance_pct
    return {"target": target, "observed_p50": observed, "deviation_pct": deviation_pct,
            "tolerance_pct": tolerance_pct, "valid": valid}


def _workload_validation(workload: WorkloadProfile, results: List[RequestResult], report: ExperimentReport) -> dict:
    """Did this class actually measure the shape it claims? Compares
    requested input tokens and the output target (max_tokens) against
    what Bedrock itself reported during the run. Output matters as much
    as input: "4096 in / 512 out" that really emitted 110 tokens is a
    different workload, and its envelope would mislead a gateway config
    for long generations."""
    spec = report.spec
    observed = _observed_tokens(results)
    inp = _deviation(observed["input_tokens_p50"], workload.input_tokens, spec.workload_validation_tolerance_pct)
    out = _deviation(observed["output_tokens_p50"], workload.output_tokens, spec.output_validation_tolerance_pct)
    checks = [v for v in (inp["valid"], out["valid"]) if v is not None]
    calibration = report.calibrations.get(workload.name)
    return {
        "token_counting": calibration.to_dict() if calibration else {"method": "estimate"},
        "input": inp,
        "output": out,
        "valid": all(checks) if checks else None,
    }


def _slo_dict(slo) -> dict:
    return {
        "ttft_p95_ms": slo.ttft_p95_ms,
        "latency_p95_ms": slo.latency_p95_ms,
        "success_rate_min": slo.success_rate_min,
        "throttle_rate_max": slo.throttle_rate_max,
        "confidence": slo.confidence,
    }


def _envelope(entry: dict, profile_report, spec) -> None:
    subject = profile_report.workload_name
    if subject in spec.provider_ceilings:
        entry["provider_constraints"] = spec.provider_ceilings[subject].to_dict()
    if spec.sweep.quota_fractions is not None:
        entry["sweep_values_rps"] = spec.sweep_values(subject)
    points = profile_report.points
    limited = [p.concurrency if p.concurrency is not None else p.rps for p in points if p.client_limited]
    if limited:
        entry["client_limited_points"] = limited
    rec = profile_report.recommendation
    if rec is None:
        analysis = profile_report.analysis
        if analysis is not None and analysis.status == "unresolved":
            entry["note"] = ("non-monotonic from the first swept value -- no stable passing region; "
                             "re-run with repetitions")
            entry["unstable_region"] = analysis.unstable_region
        else:
            entry["note"] = "no swept value met the configured SLO -- re-run with lower sweep values"
        return
    if spec.sweep.type == "concurrency":
        entry["concurrency"] = _concurrency_block(rec, headroom=spec.provider_headroom)
    else:
        entry["rate"] = _rate_block(rec, headroom=spec.provider_headroom)
    entry["evidence"] = _evidence(rec.point)


def build_capacity_profile(report: ExperimentReport) -> dict:
    spec = report.spec
    by_name = {p.workload_name: p for p in report.profiles}
    workload_classes: Dict[str, dict] = {}

    # Every defined workload gets observed tokens + validation. It gets
    # an isolated envelope only when it was swept in isolation -- a
    # class measured inside a mix has no isolated envelope to report.
    for workload in spec.workloads:
        own_results = [
            r for r in report.all_results
            if r.tags.get("workload") == workload.name and r.tags.get("measured", True)
        ]
        entry: dict = {
            "slo_profile": workload.slo_profile or spec.slo_default,
            "observed": _observed_tokens(own_results),
            "workload_validation": _workload_validation(workload, own_results, report),
        }
        profile_report = by_name.get(workload.name)
        if profile_report is not None and profile_report.mix_shares is None:
            _envelope(entry, profile_report, spec)
        workload_classes[workload.name] = entry

    mixed: Dict[str, dict] = {}
    for profile_report in report.profiles:
        if profile_report.mix_shares is None:
            continue
        entry = {"shares": {k: round(v, 4) for k, v in profile_report.mix_shares.items()}}
        _envelope(entry, profile_report, spec)
        rec = profile_report.recommendation
        if rec is not None:
            entry["classes_at_recommended_point"] = {
                name: {
                    "n": m.n, "slo_goodput_rps": m.slo_goodput_rps, "throttle_rate": m.throttle_rate,
                    "ttft_p95_ms": m.ttft_p95_ms, "latency_p95_ms": m.latency_p95_ms,
                }
                for name, m in rec.point.class_metrics.items()
            }
        mixed[profile_report.workload_name] = entry

    confidence = spec.slo.confidence or DEFAULT_CONFIDENCE
    return {
        "schema_version": 5,
        "experiment": spec.name,
        "model": {
            "name": spec.model_name,
            "provider": "bedrock",
            "model_id": spec.target.model_id,
            "region": spec.target.region,
        },
        # What every number below was judged against: the provider's
        # quota (constraints/quota.yaml) and the required SLO
        # (constraints/slo.yaml; each workload class names its profile).
        "constraints": {
            "quota": {
                "rpm": spec.quota_snapshot.rpm,
                "tpm": spec.quota_snapshot.tpm,
                "output_burndown": spec.output_burndown,
            },
            "slo": {
                "default": spec.slo_default,
                "profiles": {n: _slo_dict(c) for n, c in spec.slo_profiles.items()} or {spec.slo_default: _slo_dict(spec.slo)},
            },
        },
        "measurement": {
            "warmup_s": spec.warmup_s,
            "window_s": spec.duration_s,
            "repetitions": spec.repetitions,
            # rates/percentiles over requests scheduled in the window
            # (drain included); throughput over completions in it.
            "window_policy": "scheduled_in_window_for_rates__completed_in_window_for_throughput",
            "clock": "monotonic_durations__wall_clock_timestamps",
            "gate": "confidence_bound" if spec.slo.confidence is not None else "point_estimate",
            "min_requests_to_resolve_throttle_slo": min_samples_to_resolve_rate(
                spec.slo.throttle_rate_max, confidence=confidence,
            ),
        },
        "sweep": {
            "type": spec.sweep.type,
            # Absolute values, or quota_fractions of each subject's
            # provider ceiling (resolved per class: sweep_values_rps).
            **({"quota_fractions": spec.sweep.quota_fractions, "relative_to": "provider_ceiling"}
               if spec.sweep.quota_fractions is not None else {"values": list(spec.sweep.values)}),
        },
        "workload_classes": workload_classes,
        # Only present for a `mix:` experiment -- the one valid source
        # of a cross-class envelope, and only for THAT mix's shares.
        **({"mixed_workloads": mixed} if mixed else {}),
        "provider": {
            "headroom": spec.provider_headroom,
        },
        # Recorded for reproducibility -- what was actually running
        # when these numbers were measured (see client.py's
        # TransportConfig docstring on why this matters: SDK retry/
        # pooling/thread defaults can silently change what a sweep measures).
        "transport": {
            "max_connections": spec.transport.max_connections,
            "executor_workers": spec.transport.effective_executor_workers,
            "total_max_attempts": spec.transport.total_max_attempts,
            "connect_timeout_s": spec.transport.connect_timeout_s,
            "read_timeout_s": spec.transport.read_timeout_s,
        },
    }
