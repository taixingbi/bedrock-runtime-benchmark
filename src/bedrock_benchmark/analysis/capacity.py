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

from .metrics import DEFAULT_CONFIDENCE, RunMetrics, min_samples_to_resolve_rate


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
    # Most calls ever outstanding in the client (running + waiting for
    # a thread) during this point, and whether that exceeded the thread
    # pool -- a client_limited point measured client-side queueing, not
    # Bedrock, and can never be recommended (fails closed).
    peak_outstanding: Optional[int] = None
    client_limited: bool = False
    # "discovery" (one pass over every value) or "confirmation" (re-run
    # near the boundary with extra repetitions, pooled with discovery).
    phase: str = "discovery"


PASS, FAIL, INCONCLUSIVE = "PASS", "FAIL", "INCONCLUSIVE"


@dataclass
class Check:
    name: str  # ttft_p95 | tpot_p95 | latency_p95 | success_rate | throttle_rate | client
    verdict: str
    observed: Optional[float] = None
    threshold: Optional[float] = None
    reason: Optional[str] = None
    n: Optional[int] = None
    required_n: Optional[int] = None

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class Verdict:
    """PASS / FAIL / INCONCLUSIVE for one sweep point, with the per-check
    breakdown. Insufficient evidence is NOT failure: a point with zero
    throttles in 180 requests can't demonstrate a 0.1% throttle SLO
    (that needs ~2,700), but it didn't violate it either -- it's
    INCONCLUSIVE, and the artifact says how many requests would settle
    it. FAIL is reserved for an observed violation."""
    verdict: str
    checks: List[Check] = field(default_factory=list)

    @property
    def inconclusive_checks(self) -> List[Check]:
        return [c for c in self.checks if c.verdict == INCONCLUSIVE]

    def to_dict(self) -> dict:
        return {"verdict": self.verdict, "checks": [c.to_dict() for c in self.checks]}


def _combine(checks: List[Check]) -> str:
    verdicts = {c.verdict for c in checks}
    if FAIL in verdicts:
        return FAIL
    return INCONCLUSIVE if INCONCLUSIVE in verdicts else PASS


def _rate_check(name: str, observed: float, bound: Optional[float], limit: float, *, upper: bool,
                n: int, confidence: float) -> Check:
    """upper=True: a max-rate limit (throttle); False: a min-rate limit
    (success). Observed violation -> FAIL; the confidence bound clears
    the limit -> PASS; otherwise INCONCLUSIVE with the sample size that
    would resolve it (for a zero-event observation)."""
    violated = observed > limit if upper else observed < limit
    if violated:
        return Check(name, FAIL, observed=observed, threshold=limit, reason="observed_violation", n=n)
    if bound is not None and (bound <= limit if upper else bound >= limit):
        return Check(name, PASS, observed=observed, threshold=limit, n=n)
    tolerated = limit if upper else 1.0 - limit
    required = min_samples_to_resolve_rate(tolerated, confidence=confidence) if tolerated > 0 else None
    return Check(name, INCONCLUSIVE, observed=observed, threshold=limit, reason="insufficient_samples",
                 n=n, required_n=required)


def _latency_check(name: str, observed: Optional[float], limit: Optional[float]) -> Optional[Check]:
    if limit is None:
        return None
    # A configured latency SLO with no measurement (non-streaming TTFT,
    # < 2 output tokens for TPOT) is a missing/invalid measurement -- the
    # same fail-closed rule as before, not "inconclusive".
    if observed is None:
        return Check(name, FAIL, threshold=limit, reason="not_measured")
    return Check(name, PASS if observed <= limit else FAIL, observed=observed, threshold=limit)


def evaluate(
    metrics: RunMetrics, *,
    success_rate_min: float = 0.99, throttle_rate_max: float = 0.001,
    ttft_p95_slo_ms: Optional[float] = None, latency_p95_slo_ms: Optional[float] = None,
    tpot_p95_slo_ms: Optional[float] = None, confidence: Optional[float] = None,
) -> Verdict:
    confidence = confidence or metrics.bound_confidence or DEFAULT_CONFIDENCE
    if metrics.n == 0:
        return Verdict(FAIL, [Check("requests", FAIL, observed=0, reason="no_requests", n=0)])
    checks = [c for c in (
        _latency_check("ttft_p95", metrics.ttft_p95_ms, ttft_p95_slo_ms),
        _latency_check("tpot_p95", metrics.tpot_p95_ms, tpot_p95_slo_ms),
        _latency_check("latency_p95", metrics.latency_p95_ms, latency_p95_slo_ms),
    ) if c is not None]
    checks.append(_rate_check("success_rate", metrics.success_rate, metrics.success_rate_lower, success_rate_min,
                              upper=False, n=metrics.n, confidence=confidence))
    checks.append(_rate_check("throttle_rate", metrics.throttle_rate, metrics.throttle_rate_upper, throttle_rate_max,
                              upper=True, n=metrics.n, confidence=confidence))
    return Verdict(_combine(checks), checks)


def meets_slo(metrics: RunMetrics, *, gate_on_bounds: bool = False, **slo_kwargs) -> bool:
    """Boolean view of evaluate(): not FAIL (no observed violation), or
    with gate_on_bounds=True strictly PASS (statistically demonstrated)."""
    verdict = evaluate(metrics, **slo_kwargs).verdict
    return verdict == PASS if gate_on_bounds else verdict != FAIL


def point_verdict(point: SweepPoint, class_slo: Optional[Dict[str, dict]] = None, **slo_kwargs) -> Verdict:
    """The blend (point.metrics) is judged with slo_kwargs; each class of
    a mixed point with its own entry in class_slo (falling back to
    slo_kwargs), so every class meets ITS SLO. Check names are prefixed
    with the class for a mix."""
    if point.client_limited:
        return Verdict(FAIL, [Check("client", FAIL, observed=point.peak_outstanding, reason="client_limited")])
    blend = evaluate(point.metrics, **slo_kwargs)
    checks = list(blend.checks)
    for name, m in point.class_metrics.items():
        for c in evaluate(m, **(class_slo or {}).get(name, slo_kwargs)).checks:
            c.name = f"{name}.{c.name}"
            checks.append(c)
    return Verdict(_combine(checks), checks)


def point_meets_slo(point: SweepPoint, class_slo: Optional[Dict[str, dict]] = None, **slo_kwargs) -> bool:
    return point_verdict(point, class_slo, **slo_kwargs).verdict != FAIL


@dataclass
class SweepAnalysis:
    """Where a sweep crosses from passing to failing -- honest about
    noise. Real Bedrock sweeps aren't always monotonic (PASS, FAIL,
    PASS, FAIL can be provider noise or sampling variance), and naming
    the first failure "saturation" while recommending a point above it
    would contradict itself. So:

    - not_reached: every point passed -- the sweep never found the edge.
    - resolved:    passes then fails, cleanly -- saturation = first fail.
    - unresolved:  a pass after a fail -- no saturation is claimed;
                   stable_pass_max (end of the leading run of passes),
                   unstable_region (values between that and the last
                   pass) and confirmed_fail_from (first of the trailing
                   run of fails) describe it instead.
    """
    status: str  # "not_reached" | "resolved" | "unresolved" | "no_pass"
    stable_pass_max: Optional[float] = None
    unstable_region: List[float] = field(default_factory=list)
    confirmed_fail_from: Optional[float] = None


def _key(p: SweepPoint) -> float:
    return p.concurrency if p.concurrency is not None else p.rps


def analyze_sweep(points: List[SweepPoint], class_slo: Optional[Dict[str, dict]] = None, **slo_kwargs) -> SweepAnalysis:
    ordered = sorted(points, key=_key)
    passed = [point_meets_slo(p, class_slo, **slo_kwargs) for p in ordered]
    if not any(passed):
        return SweepAnalysis(status="no_pass")
    lead = next((i for i, ok in enumerate(passed) if not ok), len(passed))  # leading passes = ordered[:lead]
    last_pass = max(i for i, ok in enumerate(passed) if ok)
    stable_pass_max = _key(ordered[lead - 1]) if lead > 0 else None
    confirmed = _key(ordered[last_pass + 1]) if last_pass + 1 < len(ordered) else None
    if lead == len(ordered):
        return SweepAnalysis(status="not_reached", stable_pass_max=stable_pass_max)
    if last_pass < lead:
        return SweepAnalysis(status="resolved", stable_pass_max=stable_pass_max, confirmed_fail_from=confirmed)
    return SweepAnalysis(
        status="unresolved", stable_pass_max=stable_pass_max,
        unstable_region=[_key(p) for p in ordered[lead:last_pass + 1]], confirmed_fail_from=confirmed,
    )


@dataclass
class Recommendation:
    # Highest-goodput point in the leading run of non-FAIL points (PASS
    # or INCONCLUSIVE) -- the measured safe operating point.
    point: SweepPoint
    # The sweep's saturation edge -- the first point that FAILED the
    # SLO, set only when the sweep is cleanly monotonic
    # (analysis.status == "resolved"). None when every point passed
    # (not_reached: re-run with higher values) or when measurements
    # were non-monotonic (unresolved: see analysis).
    saturation_point: Optional[SweepPoint]
    analysis: SweepAnalysis = field(default_factory=lambda: SweepAnalysis(status="resolved"))
    # `point`'s verdict: PASS = statistically demonstrated; INCONCLUSIVE =
    # no violation observed but not enough requests to prove the rate SLOs.
    verdict: Verdict = field(default_factory=lambda: Verdict(PASS))
    # The statistically confirmed point, or None. Set from the DISCOVERY
    # sweep by recommend() (a fixed-sequence test: the top of the leading
    # run of strict PASSes, in ascending order) -- and, when a
    # confirmation phase runs, replaced by the executor with the result
    # of that phase alone (confirmation_source says which).
    confirmed_point: Optional[SweepPoint] = None
    confirmation_source: str = "discovery_fixed_sequence"
    # Highest swept value anywhere that didn't FAIL -- what the service
    # sustained in a short window, possibly above quota on burst capacity.
    burst_point: Optional[SweepPoint] = None


def recommend(points: List[SweepPoint], class_slo: Optional[Dict[str, dict]] = None, **slo_kwargs) -> Optional[Recommendation]:
    """Among the LEADING run of non-failing points (everything up to the
    first FAIL), picks the highest slo_goodput_rps -- ties broken toward
    the LOWER concurrency/rps. INCONCLUSIVE points are eligible (no
    violation was observed) but the recommendation carries their
    verdict. Points that pass only after an earlier failure are never
    recommended: a pass above a failure is exactly the noise a
    conservative envelope must not bet on. Returns None if no leading
    point passes -- see analyze_sweep for the why."""
    analysis = analyze_sweep(points, class_slo, **slo_kwargs)
    if analysis.stable_pass_max is None:
        return None
    ordered = sorted(points, key=_key)
    verdicts = {id(p): point_verdict(p, class_slo, **slo_kwargs) for p in ordered}

    def goodput(p: SweepPoint):
        return (p.metrics.slo_goodput_rps or 0.0, -(_key(p) or 0))

    stable = [p for p in ordered if _key(p) <= analysis.stable_pass_max]
    best = max(stable, key=goodput)
    # Fixed-sequence test over the sweep's own ascending order: each point
    # is tested at the full confidence, and the claim stops at the first
    # point that isn't a strict PASS -- so the family-wise false-PASS rate
    # stays <= alpha. (Picking the best-goodput PASS anywhere in the run,
    # skipping INCONCLUSIVE points in between, would not.)
    confirmed = []
    for p in ordered:
        if verdicts[id(p)].verdict != PASS:
            break
        confirmed.append(p)
    non_failing = [p for p in ordered if verdicts[id(p)].verdict != FAIL]
    saturation = None
    if analysis.status == "resolved":
        saturation = next(p for p in ordered if _key(p) == analysis.confirmed_fail_from)
    return Recommendation(
        point=best, saturation_point=saturation, analysis=analysis, verdict=verdicts[id(best)],
        confirmed_point=confirmed[-1] if confirmed else None,
        burst_point=max(non_failing, key=_key) if non_failing else None,
    )


def apply_headroom(value: float, *, headroom: float) -> float:
    """A recommended concurrency/rps is a MEASURED ceiling, not a
    production target -- see this repo's own README on why running a
    tenant's normal traffic right up against a measured knee is exactly
    the mistake this tool exists to prevent. headroom=0.20 means "back
    off 20% from what was measured to still pass."""
    return round(value * (1.0 - headroom), 4)
