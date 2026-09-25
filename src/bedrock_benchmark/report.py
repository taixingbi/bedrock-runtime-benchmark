"""Builds the capacity-profile.yaml artifact -- the one machine-
readable thing this repo exists to hand to bedrock-runtime-gateway's
own control-plane config review, not a human-facing HTML report. See
this repo's own README for the exact schema and the boundary this
draws: this repo outputs a safe operating ENVELOPE (min/max
concurrency per workload class); it never implements the AIMD/
admission-control/fairness logic that actually enforces it at runtime
-- that's bedrock-runtime-gateway's job.
"""
from __future__ import annotations

from typing import Dict

from .analysis.capacity import Recommendation, apply_headroom
from .experiments.executor import ExperimentReport


def build_capacity_profile(report: ExperimentReport) -> dict:
    spec = report.spec
    profiles: Dict[str, dict] = {}
    classes: Dict[str, dict] = {}
    concurrencies = []

    for profile_report in report.profiles:
        rec: Recommendation = profile_report.recommendation
        workload = next(w for w in spec.workloads if w.name == profile_report.workload_name)

        if rec is None:
            profiles[workload.name] = {
                "input_tokens": workload.input_tokens,
                "output_tokens": workload.output_tokens,
                "sustainable_rps": None,
                "recommended_concurrency": None,
                "saturation_concurrency": None,
                "note": "no swept value met the configured SLO -- re-run with lower sweep values",
            }
            continue

        saturation_value = None
        if rec.saturation_point is not None:
            saturation_value = (
                rec.saturation_point.concurrency
                if rec.saturation_point.concurrency is not None
                else rec.saturation_point.rps
            )

        sustainable_rps = rec.point.metrics.slo_goodput_rps
        if sustainable_rps is None:
            sustainable_rps = rec.point.metrics.request_throughput_rps

        profiles[workload.name] = {
            "input_tokens": workload.input_tokens,
            "output_tokens": workload.output_tokens,
            "sustainable_rps": sustainable_rps,
            "recommended_concurrency": rec.point.concurrency,
            "saturation_concurrency": saturation_value,
        }

        if rec.point.concurrency is not None:
            concurrencies.append(rec.point.concurrency)
            headroom_applied = max(1, int(apply_headroom(rec.point.concurrency, headroom=spec.provider_headroom)))
            classes[workload.name] = {"max_concurrency": headroom_applied}

    return {
        "schema_version": 1,
        "model": {
            "provider": "bedrock",
            "model_id": spec.target.model_id,
            "region": spec.target.region,
        },
        "quota": {
            "rpm": spec.quota.rpm,
            "tpm": spec.quota.tpm,
        },
        "slo": {
            "ttft_p95_ms": spec.slo.ttft_p95_ms,
            "latency_p95_ms": spec.slo.latency_p95_ms,
        },
        "profiles": profiles,
        "recommendation": {
            "provider_headroom": spec.provider_headroom,
            "gateway": {
                "global_min_concurrency": 1 if concurrencies else None,
                "global_max_concurrency": max(concurrencies) if concurrencies else None,
                "classes": classes,
            },
        },
    }
