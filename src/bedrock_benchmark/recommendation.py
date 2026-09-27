"""Admission-envelope recommendation -- the ONE place measurement turns
into policy.

A capacity profile keeps two things apart:

  measurement      what was observed and statistically confirmed (the
                   `concurrency` / `rate` blocks: observed_nonfailing_*,
                   statistically_confirmed_*, saturation, evidence)
  recommendation   that confirmed point after this benchmark's safety
                   headroom -- the admission envelope a downstream gateway
                   can take as a POLICY INPUT

It states only: "based on this measured model/workload envelope, this is
the recommended maximum backend in-flight concurrency and/or sustained
offered rate after safety headroom". It deliberately emits nothing
gateway-specific -- no global/tenant concurrency, tenant RPM limits,
queue waits, AIMD parameters or tenant allocation; mapping the envelope
onto those is the gateway's decision.

Rules (fail closed -- no statistically confirmed point, no
recommendation; an INCONCLUSIVE point is never used):

  concurrency sweep   max_inflight  = floor(confirmed_concurrency x (1 - headroom))
  rate sweep          measurement   = confirmed_offered_rps x (1 - headroom)
                      quota         = provider_ceiling_rps x (1 - quota_headroom)
                      sustained_rps = min(measurement, quota)

A max_inflight that floors to 0 (confirmed concurrency too small for the
headroom, e.g. 1 x 0.8) is no recommendation either: 0 would admit
nothing, and rounding up would silently drop the headroom.

Rounding and the quota cap change the margin actually applied, so every
envelope states both: headroom_fraction (the policy target) and
effective_headroom_fraction (1 - recommended / confirmed). At small
concurrency they differ a lot: confirmed C=2 -> floor(1.6) = 1 is a 50%
margin, not 20%.
"""
from __future__ import annotations

import math
from typing import Optional

SOURCE = "statistically_confirmed_measurement"

_NO_CONFIRMED = ("no statistically confirmed point -- nothing is recommended from an observed-only or "
                 "INCONCLUSIVE point; collect more samples rather than relax the SLO")


def _headroom(value: float, fraction: float) -> float:
    return round(value * (1.0 - fraction), 4)


def _effective(recommended: float, confirmed: float) -> float:
    return round(1.0 - recommended / confirmed, 4)


def admission_envelope(
    sweep_type: str, confirmed: Optional[float], *, headroom: float, quota_headroom: float = 0.0,
    provider_ceiling_rps: Optional[float] = None, unconfirmed_reason: Optional[str] = None,
    scope: str = "isolated_workload_class",
) -> dict:
    """The `recommendation` block for one workload class or mix.
    `confirmed` is the statistically confirmed concurrency (concurrency
    sweep) or offered rps (rate sweep) -- None when nothing was confirmed,
    in which case `unconfirmed_reason` (the measurement-specific cause,
    see report.py) replaces the generic reason.

    `scope` says what traffic the envelope holds for:
    isolated_workload_class -- that class running ALONE; per-class values
    are NOT additive and are not a global limit. workload_mix -- the
    measured mix at its measured shares, as one total."""
    if confirmed is None:
        return {"admission_envelope": None, "reason": unconfirmed_reason or _NO_CONFIRMED}

    if sweep_type == "concurrency":
        max_inflight = math.floor(confirmed * (1.0 - headroom) + 1e-9)
        if max_inflight < 1:
            return {"admission_envelope": None,
                    "reason": f"statistically confirmed concurrency {confirmed:g} leaves < 1 in-flight request "
                              f"after {headroom:.0%} headroom -- confirm a higher concurrency to recommend one"}
        return {"admission_envelope": {
            "max_inflight": max_inflight,
            "sustained_rps": None,
            "scope": scope,
            "source": SOURCE,
            "headroom_fraction": headroom,                               # policy target
            "effective_headroom_fraction": _effective(max_inflight, confirmed),  # after rounding
            "rounding_policy": "floor",
            "basis": {"statistically_confirmed_concurrency": confirmed},
        }}

    if sweep_type == "rate":
        from_measurement = _headroom(confirmed, headroom)
        from_quota = _headroom(provider_ceiling_rps, quota_headroom) if provider_ceiling_rps else None
        if from_quota is not None and from_quota < from_measurement:
            sustained, binding = from_quota, "provider_quota"
        else:
            sustained, binding = from_measurement, "measurement"
        return {"admission_envelope": {
            "max_inflight": None,
            "sustained_rps": sustained,
            "scope": scope,
            "source": SOURCE,
            "headroom_fraction": headroom,
            "quota_headroom_fraction": quota_headroom,
            "effective_headroom_fraction": _effective(sustained, confirmed),  # off the confirmed rate
            "binding": binding,  # measurement | provider_quota
            "basis": {
                "statistically_confirmed_offered_rps": confirmed,
                "provider_ceiling_rps": None if provider_ceiling_rps is None else round(provider_ceiling_rps, 4),
            },
        }}

    raise ValueError(f"unknown sweep type {sweep_type!r}")
