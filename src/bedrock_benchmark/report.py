"""Builds the capacity-profile.yaml artifact (schema_version 3) -- the
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


def _concurrency_block(rec: Recommendation, *, headroom: float) -> dict:
    saturation = rec.saturation_point.concurrency if rec.saturation_point is not None else None
    production_max = max(1, int(apply_headroom(rec.point.concurrency, headroom=headroom)))
    return {
        "measured_best": rec.point.concurrency,
        "saturation": saturation,
        "production_max": production_max,
        "slo_goodput_rps": rec.point.metrics.slo_goodput_rps,
    }


def _rate_block(rec: Recommendation, *, headroom: float) -> dict:
    saturation_rps = rec.saturation_point.rps if rec.saturation_point is not None else None
    return {
        "max_safe_offered_rps": rec.point.rps,
        "slo_goodput_rps": rec.point.metrics.slo_goodput_rps,
        "saturation_offered_rps": saturation_rps,
        "production_offered_rps": apply_headroom(rec.point.rps, headroom=headroom),
    }


def _evidence(m: RunMetrics) -> dict:
    return {
        "n": m.n,
        "n_throttled": m.n_throttled,
        "measured_duration_s": m.measured_duration_s,
        "throttle_rate": m.throttle_rate,
        "throttle_rate_upper": m.throttle_rate_upper,
        "success_rate": m.success_rate,
        "success_rate_lower": m.success_rate_lower,
        "bound_confidence": m.bound_confidence,
    }


def _workload_validation(workload: WorkloadProfile, results: List[RequestResult], report: ExperimentReport) -> dict:
    """Did this class actually measure the shape it claims? Compares
    the REQUESTED input tokens against what Bedrock itself reported
    during the run -- the check on the 4-chars/token padding estimate."""
    spec = report.spec
    observed = _observed_tokens(results)["input_tokens_p50"]
    deviation_pct = None
    valid = None
    if observed is not None and workload.input_tokens > 0:
        deviation_pct = round((observed - workload.input_tokens) / workload.input_tokens * 100, 2)
        valid = abs(deviation_pct) <= spec.workload_validation_tolerance_pct
    return {
        "requested_input_tokens": workload.input_tokens,
        "padding": "4_chars_per_token_estimate",
        "observed_input_tokens_p50": observed,
        "deviation_pct": deviation_pct,
        "tolerance_pct": spec.workload_validation_tolerance_pct,
        "valid": valid,
    }


def _envelope(entry: dict, rec: Optional[Recommendation], spec) -> None:
    if rec is None:
        entry["note"] = "no swept value met the configured SLO -- re-run with lower sweep values"
        return
    if spec.sweep.type == "concurrency":
        entry["concurrency"] = _concurrency_block(rec, headroom=spec.provider_headroom)
    else:
        entry["rate"] = _rate_block(rec, headroom=spec.provider_headroom)
    entry["evidence"] = _evidence(rec.point.metrics)
    if len(rec.point.repetitions) > 1:
        entry["evidence"]["repetition_slo_goodput_rps"] = [m.slo_goodput_rps for m in rec.point.repetitions]


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
            "observed": _observed_tokens(own_results),
            "workload_validation": _workload_validation(workload, own_results, report),
        }
        profile_report = by_name.get(workload.name)
        if profile_report is not None and profile_report.mix_shares is None:
            _envelope(entry, profile_report.recommendation, spec)
        workload_classes[workload.name] = entry

    mixed: Dict[str, dict] = {}
    for profile_report in report.profiles:
        if profile_report.mix_shares is None:
            continue
        entry = {"shares": {k: round(v, 4) for k, v in profile_report.mix_shares.items()}}
        rec = profile_report.recommendation
        _envelope(entry, rec, spec)
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
        "schema_version": 3,
        "experiment": spec.name,
        "model": {
            "name": spec.model_name,
            "provider": "bedrock",
            "model_id": spec.target.model_id,
            "region": spec.target.region,
        },
        "quota_snapshot": {
            "rpm": spec.quota_snapshot.rpm,
            "tpm": spec.quota_snapshot.tpm,
        },
        "slo": {
            "ttft_p95_ms": spec.slo.ttft_p95_ms,
            "latency_p95_ms": spec.slo.latency_p95_ms,
            "success_rate_min": spec.slo.success_rate_min,
            "throttle_rate_max": spec.slo.throttle_rate_max,
            "confidence": spec.slo.confidence,
        },
        "measurement": {
            "warmup_s": spec.warmup_s,
            "window_s": spec.duration_s,
            "repetitions": spec.repetitions,
            # rates/percentiles over requests scheduled in the window
            # (drain included); throughput over completions in it.
            "window_policy": "scheduled_in_window_for_rates__completed_in_window_for_throughput",
            "gate": "confidence_bound" if spec.slo.confidence is not None else "point_estimate",
            "min_requests_to_resolve_throttle_slo": min_samples_to_resolve_rate(
                spec.slo.throttle_rate_max, confidence=confidence,
            ),
        },
        "sweep": {
            "type": spec.sweep.type,
            "values": list(spec.sweep.values),
            # Set for a quota-relative rate sweep: values = fraction x rpm/60.
            **({"quota_fractions": spec.sweep.quota_fractions} if spec.sweep.quota_fractions else {}),
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
        # pooling defaults can silently change what a sweep measures).
        "transport": {
            "max_connections": spec.transport.max_connections,
            "total_max_attempts": spec.transport.total_max_attempts,
            "connect_timeout_s": spec.transport.connect_timeout_s,
            "read_timeout_s": spec.transport.read_timeout_s,
        },
    }
