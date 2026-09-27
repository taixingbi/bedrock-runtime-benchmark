"""Measurement analysis -- deliberately rule-based, not a "black box"
score: every number must say exactly why it is what it is.

    capacity = the highest STATISTICALLY CONFIRMED SLO-compliant
               operating point

The pipeline:

    discovery sweep  -> PASS / FAIL / INCONCLUSIVE per point (evaluate)
                     -> observed_nonfailing (highest point before the
                        first FAIL), saturation (first FAIL), candidates
    confirmation     -> statistically_confirmed = the capacity, from
                        fresh independent data (confirmation.py)

SLO goodput is reported at every point as an OBSERVED metric; it never
selects anything. No headroom is applied here -- this module is
measurement only; policy (headroom -> admission envelope) lives in
recommendation.py.

Never: fit a curve, guess a knee, or otherwise infer a number no single
measured point actually produced.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .metrics import (
    DEFAULT_CONFIDENCE, RunMetrics, min_samples_to_resolve_rate, quantile_upper_bound, rate_upper, required_samples,
)


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

# A p95 latency SLO "p95 <= T" is the same statement as "at most 5% of
# requests exceed T" -- judged here as an exceedance PROPORTION with the
# same exact binomial bound as the throttle / success checks.
LATENCY_QUANTILE = 0.95
LATENCY_EXCEEDANCE_MAX = round(1.0 - LATENCY_QUANTILE, 10)


@dataclass
class Check:
    name: str  # ttft_p95 | tpot_p95 | latency_p95 | success_rate | throttle_rate | client
    verdict: str
    observed: Optional[float] = None
    threshold: Optional[float] = None
    reason: Optional[str] = None
    n: Optional[int] = None
    required_n: Optional[int] = None
    # Latency checks: requests over the threshold, and the upper bound on
    # that proportion (the check is PASS when it's <= 5%).
    exceedances: Optional[int] = None
    exceedance_rate_upper: Optional[float] = None
    # Latency checks: one-sided upper confidence bound on the TRUE p95
    # (order statistic) -- "p95 estimate 742ms, 95% UCB 796ms <= 800ms".
    # None when n is too small for any sample to bound it.
    p95_upper_bound: Optional[float] = None

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
                n: int, confidence: float, exact: Optional[float] = None) -> Check:
    """upper=True: a max-rate limit (throttle); False: a min-rate limit
    (success). Observed violation -> FAIL; the confidence bound clears
    the limit -> PASS; otherwise INCONCLUSIVE with the sample size that
    would resolve it (for a zero-event observation). The violation test
    uses `exact` (count / n) when given -- `observed` is the rounded
    value for the report, and rounding must never decide a verdict."""
    rate = exact if exact is not None else observed
    violated = rate > limit if upper else rate < limit
    if violated:
        return Check(name, FAIL, observed=observed, threshold=limit, reason="observed_violation", n=n)
    if bound is not None and (bound <= limit if upper else bound >= limit):
        return Check(name, PASS, observed=observed, threshold=limit, n=n)
    tolerated = limit if upper else 1.0 - limit
    required = min_samples_to_resolve_rate(tolerated, confidence=confidence) if tolerated > 0 else None
    return Check(name, INCONCLUSIVE, observed=observed, threshold=limit, reason="insufficient_samples",
                 n=n, required_n=required)


def _latency_check(name: str, key: str, metrics: RunMetrics, limit: Optional[float],
                   confidence: float) -> Optional[Check]:
    """p95 <= limit, proven rather than just observed -- H0: q95 > limit,
    H1: q95 <= limit, tested distribution-free. With k of n
    successful requests over `limit` (a request with no measurement
    counts as over -- not measured is not compliant):

        k / n > 5%                        -> FAIL (the sample p95 is over)
        exact upper bound on k / n <= 5%  -> PASS
        otherwise                         -> INCONCLUSIVE, with required_n

    30 requests all under the limit still bound the exceedance at ~9.5%,
    so they're INCONCLUSIVE, not a PASS; 59 clean requests resolve it.
    The check also reports p95_upper_bound, the order-statistic UCB on the
    true p95 -- the same test read in milliseconds (PASS <=> UCB <= limit)."""
    if limit is None:
        return None
    observed = {"ttft": metrics.ttft_p95_ms, "tpot": metrics.tpot_p95_ms, "latency": metrics.latency_p95_ms}[key]
    samples = (metrics.latency_samples or {}).get(key)
    if samples is None:
        # Hand-built metrics without per-request samples (tests only --
        # compute_run_metrics always provides them): sample percentile.
        if observed is None:
            return Check(name, FAIL, threshold=limit, reason="not_measured")
        return Check(name, PASS if observed <= limit else FAIL, observed=observed, threshold=limit)
    n = len(samples)
    if n == 0 or all(v is None for v in samples):
        # A configured latency SLO with no measurement at all (non-
        # streaming TTFT, < 2 output tokens for TPOT) fails closed.
        return Check(name, FAIL, threshold=limit, reason="not_measured", n=n)
    k = sum(1 for v in samples if v is None or v > limit)
    bound = round(rate_upper(k, n, confidence=confidence), 6)
    ucb = quantile_upper_bound(samples, LATENCY_QUANTILE, confidence=confidence)
    common = dict(observed=observed, threshold=limit, n=n, exceedances=k, exceedance_rate_upper=bound,
                  p95_upper_bound=None if ucb is None else round(ucb, 3))
    if k / n > LATENCY_EXCEEDANCE_MAX:
        return Check(name, FAIL, reason="observed_violation", **common)
    if bound <= LATENCY_EXCEEDANCE_MAX:
        return Check(name, PASS, **common)
    return Check(name, INCONCLUSIVE, reason="insufficient_samples",
                 required_n=required_samples(k, LATENCY_EXCEEDANCE_MAX, confidence=confidence), **common)


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
        _latency_check("ttft_p95", "ttft", metrics, ttft_p95_slo_ms, confidence),
        _latency_check("tpot_p95", "tpot", metrics, tpot_p95_slo_ms, confidence),
        _latency_check("latency_p95", "latency", metrics, latency_p95_slo_ms, confidence),
    ) if c is not None]
    exact_success = metrics.n_success / metrics.n if metrics.n_success is not None else None
    exact_throttle = metrics.n_throttled / metrics.n if metrics.n_success is not None else None
    checks.append(_rate_check("success_rate", metrics.success_rate, metrics.success_rate_lower, success_rate_min,
                              upper=False, n=metrics.n, confidence=confidence, exact=exact_success))
    checks.append(_rate_check("throttle_rate", metrics.throttle_rate, metrics.throttle_rate_upper, throttle_rate_max,
                              upper=True, n=metrics.n, confidence=confidence, exact=exact_throttle))
    return Verdict(_combine(checks), checks)


def meets_slo(metrics: RunMetrics, *, gate_on_bounds: bool = False, **slo_kwargs) -> bool:
    """Boolean view of evaluate(): not FAIL (no observed violation), or
    with gate_on_bounds=True strictly PASS (statistically demonstrated)."""
    verdict = evaluate(metrics, **slo_kwargs).verdict
    return verdict == PASS if gate_on_bounds else verdict != FAIL


def point_verdict(point: SweepPoint, class_slo: Optional[Dict[str, dict]] = None, **slo_kwargs) -> Verdict:
    """An isolated point is judged on its metrics with slo_kwargs.

    A MIXED point (class_slo given) is judged ONLY per class, each on its
    own service-class SLO: PASS = every class PASSes. The blend is
    reported, never gated -- gating it on the strictest class's success
    / throttle limit would add a constraint no class has: 60% gold at
    99.5% + 40% silver/bronze at 99.0% blend to ~99.3%, which would FAIL
    a 99.5% blend gate while every class meets its own SLO. Check names
    are prefixed with the class."""
    if point.client_limited:
        return Verdict(FAIL, [Check("client", FAIL, observed=point.peak_outstanding, reason="client_limited")])
    mixed = class_slo is not None and bool(point.class_metrics)
    checks = [] if mixed else list(evaluate(point.metrics, **slo_kwargs).checks)
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
    # OBSERVED: the highest point in the leading run of non-FAIL points
    # (PASS or INCONCLUSIVE). Not the capacity -- see confirmed_point.
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
    # CAPACITY: the highest statistically confirmed SLO-compliant
    # operating point, or None. SLO goodput never selects it -- it's an
    # observed metric only. Set from the DISCOVERY
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
    """capacity = the highest STATISTICALLY CONFIRMED SLO-compliant
    operating point (confirmed_point). Alongside it, the OBSERVED point:
    the highest point of the leading run of non-failing points
    (everything up to the first FAIL; INCONCLUSIVE counts as non-failing
    and the verdict is carried along). SLO goodput is reported, never
    used to select either. Points that pass only after an earlier
    failure are never used: a pass above a failure is exactly the noise
    a conservative envelope must not bet on. Returns None if no leading
    point is non-failing -- see analyze_sweep for the why."""
    analysis = analyze_sweep(points, class_slo, **slo_kwargs)
    if analysis.stable_pass_max is None:
        return None
    ordered = sorted(points, key=_key)
    verdicts = {id(p): point_verdict(p, class_slo, **slo_kwargs) for p in ordered}

    stable = [p for p in ordered if _key(p) <= analysis.stable_pass_max]
    best = max(stable, key=_key)
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
