"""Confirmation phase -- the ONLY source of a statistically confirmed
operating point. Discovery data picks candidates; fresh, independent
confirmation data decides whether a candidate meets its SLO.

Why a separate module: the statistics here are what make a PASS mean
something, so they live apart from the Bedrock I/O loop (executor.py)
and are testable on their own.

Two rules keep the false-PASS rate at or below alpha = 1 - confidence:

1. No double-dipping. The candidate is chosen because its DISCOVERY data
   looked good; confirming it with that same data would be biased. So
   confirmation verdicts use confirmation data only -- discovery data is
   never pooled in.

2. No unplanned looks. Re-checking a confidence bound after every repetition
   and stopping the first time it clears is optional stopping: given
   enough looks, noise alone eventually produces a PASS. So PASS can be
   declared only at L sample sizes fixed BEFORE any confirmation data
   exists (the look schedule), each at confidence 1 - alpha / L
   (Bonferroni): P(false PASS at any look) <= L x alpha / L = alpha.
   The per-look test is the EXACT Clopper-Pearson bound, so each look's
   error really is <= alpha / L (Wilson would not guarantee that at the
   0-2 events gold operates at).
   Look j's sample size is the smallest n at which j - 1 observed bad
   events would still clear the limit -- so later looks exist to absorb
   a stray throttle, not to retry until lucky.

   FAIL may be declared at any time (an observed violation): stopping
   to fail can never create a false PASS.

Caps (repetitions / requests per candidate, wall time for the phase)
bound the cost. Reaching a cap without a PASS is INCONCLUSIVE -- the
SLO is never relaxed to reach a verdict. A candidate whose first look
can't be reached within the caps (estimated from discovery's
requests-per-repetition) is reported `unreachable_within_caps` without
spending the calls.

Several candidates are tested lowest-first and stop at the first one not
confirmed (a fixed-sequence procedure): a false claim requires the first
unsafe point in the sequence to falsely PASS, so the family-wise error
stays <= alpha without splitting it further.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .capacity import FAIL, INCONCLUSIVE, PASS, SweepPoint, Verdict
from .metrics import rate_upper


def required_samples(bad_events: int, max_rate: float, *, confidence: float) -> int:
    """Smallest n at which `bad_events` observed in n requests gives a
    exact (Clopper-Pearson) one-sided upper bound <= max_rate. (A success-rate floor is
    the same question on failures: max_rate = 1 - success_rate_min.)"""
    if max_rate <= 0:
        raise ValueError("max_rate must be > 0")
    if max_rate >= 1:
        return max(1, bad_events)
    lo = max(1, bad_events)
    hi = lo
    while rate_upper(bad_events, hi, confidence=confidence) > max_rate:
        hi *= 2
        if hi > 10**9:
            raise ValueError("required sample size exceeds 1e9")
    while lo < hi:  # the bound decreases in n for fixed events
        mid = (lo + hi) // 2
        if rate_upper(bad_events, mid, confidence=confidence) <= max_rate:
            hi = mid
        else:
            lo = mid + 1
    return lo


@dataclass
class RateLimit:
    """One rate check to plan for. share: the fraction of the candidate's
    requests this check sees (1.0 for the blend, a class's mix share)."""
    name: str
    max_bad_rate: float  # throttle_rate_max, or 1 - success_rate_min
    share: float = 1.0


@dataclass
class ConfirmationPlan:
    confidence: float           # the SLO's (family-wise) confidence, e.g. 0.95
    max_looks: int              # L
    per_look_confidence: float  # 1 - (1 - confidence) / L
    look_schedule: List[int]    # N_1 < ... < N_L total confirmation requests
    max_repetitions: int
    max_requests: int
    max_duration_s: float

    def to_dict(self) -> dict:
        return {
            "confidence": self.confidence, "max_looks": self.max_looks,
            "per_look_confidence": round(self.per_look_confidence, 6),
            "look_schedule_requests": self.look_schedule,
            "caps": {"max_repetitions": self.max_repetitions, "max_requests": self.max_requests,
                     "max_duration_s": self.max_duration_s},
        }


def plan_looks(limits: List[RateLimit], *, confidence: float, max_looks: int, max_repetitions: int,
               max_requests: int, max_duration_s: float) -> ConfirmationPlan:
    """Look j (1-based) is at the total request count where every rate
    check could still PASS with j - 1 bad events of its own -- fixed
    before any confirmation data exists."""
    per_look = 1.0 - (1.0 - confidence) / max_looks
    schedule = []
    for j in range(max_looks):
        n = max(math.ceil(required_samples(j, lim.max_bad_rate, confidence=per_look) / lim.share) for lim in limits)
        schedule.append(max(n, schedule[-1] + 1) if schedule else n)
    return ConfirmationPlan(
        confidence=confidence, max_looks=max_looks, per_look_confidence=per_look, look_schedule=schedule,
        max_repetitions=max_repetitions, max_requests=max_requests, max_duration_s=max_duration_s,
    )


@dataclass
class ConfirmationResult:
    value: float                 # the candidate's concurrency or rps
    verdict: str                 # PASS | FAIL | INCONCLUSIVE
    stop_reason: str             # confirmed | observed_violation | looks_exhausted | max_repetitions |
                                 # max_requests | max_duration | unreachable_within_caps | not_tested
    repetitions: int = 0
    n: int = 0
    looks_used: int = 0
    next_look_n: Optional[int] = None  # the look it was working toward when it stopped
    detail: Optional[Verdict] = None   # checks at the last evaluation (per-look confidence)
    point: Optional[SweepPoint] = None # confirmation-only data -- never discovery

    def to_dict(self) -> dict:
        out = {"value": self.value, "verdict": self.verdict, "stop_reason": self.stop_reason,
               "repetitions": self.repetitions, "n": self.n, "looks_used": self.looks_used}
        if self.next_look_n is not None and self.verdict != PASS:
            out["next_look_n"] = self.next_look_n
        if self.detail is not None:
            out["checks"] = [c.to_dict() for c in self.detail.checks if c.verdict != PASS] or "all PASS"
        if self.point is not None:
            m = self.point.metrics
            out["metrics"] = {"n_throttled": m.n_throttled, "throttle_rate_upper": m.throttle_rate_upper,
                              "success_rate_lower": m.success_rate_lower, "ttft_p95_ms": m.ttft_p95_ms,
                              "tpot_p95_ms": m.tpot_p95_ms, "latency_p95_ms": m.latency_p95_ms,
                              "slo_goodput_rps": m.slo_goodput_rps}
        return out


def step(verdict: Verdict, n: int, looks_used: int, plan: ConfirmationPlan) -> Optional[tuple]:
    """Decide after one confirmation repetition. `verdict` must come from
    metrics whose bounds were computed at plan.per_look_confidence.
    Returns (verdict, stop_reason, looks_used) to stop, or None to keep
    measuring. Looks are taken only when n crosses the next scheduled
    sample size; FAIL is honored at any time."""
    if verdict.verdict == FAIL:
        return FAIL, "observed_violation", looks_used
    while looks_used < plan.max_looks and n >= plan.look_schedule[looks_used]:
        looks_used += 1  # this scheduled look is spent whether or not it passes
        if verdict.verdict == PASS:
            return PASS, "confirmed", looks_used
    if looks_used >= plan.max_looks:
        return INCONCLUSIVE, "looks_exhausted", looks_used
    return None


def reachable(plan: ConfirmationPlan, *, est_requests_per_rep: float, remaining_duration_s: float,
              per_rep_s: float, looks_used: int = 0) -> bool:
    """Can the next scheduled look be reached within the caps?"""
    if looks_used >= plan.max_looks or est_requests_per_rep <= 0:
        return False
    reps_by_time = int(remaining_duration_s // per_rep_s) if per_rep_s > 0 else plan.max_repetitions
    max_n = min(plan.max_requests, est_requests_per_rep * min(plan.max_repetitions, reps_by_time))
    return max_n >= plan.look_schedule[looks_used]


def fixed_sequence_confirmed(results: List[ConfirmationResult]) -> Optional[ConfirmationResult]:
    """Candidates are tested in ascending order and stop at the first
    non-PASS; the confirmed point is the highest one in that leading
    run of PASSes."""
    confirmed = None
    for r in sorted(results, key=lambda r: r.value):
        if r.verdict != PASS:
            break
        confirmed = r
    return confirmed


def limits_for(gate_kwargs: dict, class_gate: Optional[Dict[str, dict]], shares: Optional[Dict[str, float]]) -> List[RateLimit]:
    """The rate checks a candidate's verdict depends on: the blend's, and
    each mixed class's (seeing only its share of the requests)."""
    def of(prefix: str, kw: dict, share: float) -> List[RateLimit]:
        out = []
        if kw.get("throttle_rate_max", 0) > 0:
            out.append(RateLimit(f"{prefix}throttle_rate", kw["throttle_rate_max"], share))
        if kw.get("success_rate_min", 0) < 1:
            out.append(RateLimit(f"{prefix}success_rate", 1.0 - kw["success_rate_min"], share))
        return out

    limits = of("", gate_kwargs, 1.0)
    for name, kw in (class_gate or {}).items():
        limits += of(f"{name}.", kw, (shares or {}).get(name, 1.0))
    return limits
