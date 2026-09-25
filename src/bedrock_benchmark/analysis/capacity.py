"""Recommendation engine, deliberately rule-based, not a "black box"
score: a config recommendation this repo hands to a gateway's own
control-plane config MUST be able to say exactly why it picked what it
picked. The pipeline is always:

    measurements -> SLO filtering -> pick max SLO-goodput among the
    survivors -> apply headroom -> recommended operating envelope

Never: fit a curve, guess a knee, or otherwise infer a number no single
measured point actually produced.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .metrics import RunMetrics


@dataclass
class SweepPoint:
    """One measured point in a sweep -- exactly one of concurrency/rps
    is set, matching which runner produced it (ConcurrencyRunner vs
    RateRunner)."""
    concurrency: Optional[int]
    rps: Optional[float]
    # Pooled across every repetition's measurement window -- what the
    # SLO gate and the recommendation read.
    metrics: RunMetrics
    # One entry per repetition, kept so run-to-run spread is visible
    # instead of hidden inside the pooled number.
    repetitions: List[RunMetrics] = field(default_factory=list)
    # Mixed-workload sweeps only: pooled metrics per drawn class. The
    # SLO must hold for EVERY class, not just the blend -- a 70/30
    # short/long mix can have a fine aggregate p95 while the long
    # class alone blows its latency SLO.
    class_metrics: Dict[str, RunMetrics] = field(default_factory=dict)


def meets_slo(
    metrics: RunMetrics, *,
    success_rate_min: float = 0.99, throttle_rate_max: float = 0.001,
    ttft_p95_slo_ms: Optional[float] = None, latency_p95_slo_ms: Optional[float] = None,
    gate_on_bounds: bool = False,
) -> bool:
    """gate_on_bounds=True gates the two rate SLOs on their confidence
    bounds (success_rate_lower / throttle_rate_upper) instead of the
    raw point estimates -- a point must DEMONSTRATE it meets the SLO
    at the measured sample size, not merely fail to observe a
    violation. Too few requests then fails closed, the same way a
    configured-but-unmeasured TTFT SLO does below."""
    if metrics.n == 0:
        return False
    success_rate = metrics.success_rate
    throttle_rate = metrics.throttle_rate
    if gate_on_bounds:
        if metrics.success_rate_lower is None or metrics.throttle_rate_upper is None:
            return False
        success_rate = metrics.success_rate_lower
        throttle_rate = metrics.throttle_rate_upper
    if success_rate < success_rate_min:
        return False
    if throttle_rate > throttle_rate_max:
        return False
    # A configured TTFT SLO with no TTFT measurement at all (e.g.
    # stream: false, or every streaming call failed before its first
    # token) is a missing/invalid measurement, not a pass -- same fix
    # as metrics.py's per-request meets_slo, and for the same reason:
    # the old form silently skipped the check when ttft_p95_ms was None.
    if ttft_p95_slo_ms is not None and (metrics.ttft_p95_ms is None or metrics.ttft_p95_ms > ttft_p95_slo_ms):
        return False
    if latency_p95_slo_ms is not None and metrics.latency_p95_ms > latency_p95_slo_ms:
        return False
    return True


def point_meets_slo(point: SweepPoint, **slo_kwargs) -> bool:
    return meets_slo(point.metrics, **slo_kwargs) and all(
        meets_slo(m, **slo_kwargs) for m in point.class_metrics.values()
    )


@dataclass
class Recommendation:
    point: SweepPoint
    # The sweep's own saturation edge -- the smallest point (by
    # concurrency/rps) that FAILED the SLO, i.e. where things first
    # broke. None if every swept point passed (the sweep never actually
    # found the ceiling -- worth re-running with higher values, not
    # silently treated as "no ceiling exists").
    saturation_point: Optional[SweepPoint]


def recommend(points: List[SweepPoint], **slo_kwargs) -> Optional[Recommendation]:
    """Among points satisfying meets_slo, picks the one with the
    highest slo_goodput_rps -- ties broken toward the LOWER
    concurrency/rps (a conservative choice: no reason to run hotter for
    the same goodput). Returns None if no swept point meets the SLO at
    all -- a real, worth-surfacing result (this workload may not be
    safely servable under this SLO at any of the swept values), not
    silently recommending the least-bad option."""
    passing = [p for p in points if point_meets_slo(p, **slo_kwargs)]
    failing = [p for p in points if not point_meets_slo(p, **slo_kwargs)]

    def sort_key(p: SweepPoint):
        return p.concurrency if p.concurrency is not None else p.rps

    saturation_point = min(failing, key=sort_key) if failing else None

    if not passing:
        return None

    best = max(passing, key=lambda p: (p.metrics.slo_goodput_rps or 0.0, -(sort_key(p) or 0)))
    return Recommendation(point=best, saturation_point=saturation_point)


def apply_headroom(value: float, *, headroom: float) -> float:
    """A recommended concurrency/rps is a MEASURED ceiling, not a
    production target -- see this repo's own README on why running a
    tenant's normal traffic right up against a measured knee is exactly
    the mistake this tool exists to prevent. headroom=0.20 means "back
    off 20% from what was measured to still pass."""
    return round(value * (1.0 - headroom), 4)
