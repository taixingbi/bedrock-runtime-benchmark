"""Builds the capacity-profile.yaml artifact (schema_version 2) -- the
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

See this repo's own README for the boundary this draws: this repo
outputs a safe operating envelope per workload class; it never
implements or pre-decides a gateway's global/tenant/AIMD control
policy.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from .analysis.capacity import Recommendation, apply_headroom
from .analysis.metrics import percentile
from .experiments.executor import ExperimentReport
from .results import RequestResult


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
    }


def _rate_block(rec: Recommendation, *, headroom: float) -> dict:
    sustainable_rps: Optional[float] = rec.point.metrics.slo_goodput_rps
    if sustainable_rps is None:
        sustainable_rps = rec.point.metrics.request_throughput_rps
    saturation_rps = rec.saturation_point.rps if rec.saturation_point is not None else None
    return {
        "measured_sustainable_rps": sustainable_rps,
        "saturation_rps": saturation_rps,
        "production_rps": apply_headroom(sustainable_rps, headroom=headroom),
    }


def build_capacity_profile(report: ExperimentReport) -> dict:
    spec = report.spec
    workload_classes: Dict[str, dict] = {}

    for profile_report in report.profiles:
        workload = next(w for w in spec.workloads if w.name == profile_report.workload_name)
        own_results = [r for r in report.all_results if r.tags.get("workload") == workload.name]
        entry: dict = {"observed": _observed_tokens(own_results)}

        rec = profile_report.recommendation
        if rec is None:
            entry["note"] = "no swept value met the configured SLO -- re-run with lower sweep values"
        elif spec.sweep.type == "concurrency":
            entry["concurrency"] = _concurrency_block(rec, headroom=spec.provider_headroom)
        elif spec.sweep.type == "rate":
            entry["rate"] = _rate_block(rec, headroom=spec.provider_headroom)

        workload_classes[workload.name] = entry

    return {
        "schema_version": 2,
        "model": {
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
        },
        "workload_classes": workload_classes,
        "provider": {
            "headroom": spec.provider_headroom,
        },
        # Recorded for reproducibility -- what was actually running
        # when these numbers were measured (see client.py's
        # TransportConfig docstring on why this matters: SDK retry/
        # pooling defaults can silently change what a sweep measures).
        "transport": {
            "max_connections": spec.transport.max_connections,
            "retry_max_attempts": spec.transport.retry_max_attempts,
            "connect_timeout_s": spec.transport.connect_timeout_s,
            "read_timeout_s": spec.transport.read_timeout_s,
        },
    }
