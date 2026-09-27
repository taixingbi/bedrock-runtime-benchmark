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

import platform
import subprocess
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path
from typing import Dict, List, Optional

from .analysis.capacity import FAIL, INCONCLUSIVE, PASS, Recommendation
from .analysis.metrics import DEFAULT_CONFIDENCE, RunMetrics, min_samples_to_resolve_rate, percentile
from .experiments.executor import ExperimentReport
from .recommendation import admission_envelope
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


def _value(point) -> float:
    return point.concurrency if point.concurrency is not None else point.rps


def _observed_fields(rec: Recommendation) -> dict:
    """What was OBSERVED: the best point with no FAIL before the first
    failure. Its verdict may be INCONCLUSIVE -- no violation seen, not
    enough requests to prove the SLO -- which is exactly why it is never
    used to derive a production value."""
    out: dict = {"observed_verdict": rec.verdict.verdict}
    if rec.verdict.inconclusive_checks:
        out["observed_inconclusive_checks"] = [c.to_dict() for c in rec.verdict.inconclusive_checks]
    return out


def _concurrency_block(rec: Recommendation) -> dict:
    """MEASUREMENT only: observed_nonfailing -> statistically_confirmed.
    The policy step (headroom -> max_inflight) lives in the entry's
    `recommendation` block (recommendation.py), never here."""
    saturation = rec.saturation_point.concurrency if rec.saturation_point is not None else None
    confirmed = rec.confirmed_point.concurrency if rec.confirmed_point is not None else None
    out = {
        "observed_nonfailing": rec.point.concurrency,
        **_observed_fields(rec),
        "statistically_confirmed": confirmed,
        "confirmation_source": rec.confirmation_source,
        "saturation": saturation,
        **_saturation_fields(rec),
        "observed_slo_goodput_rps": rec.point.metrics.slo_goodput_rps,
    }
    return out


def _rate_block(rec: Recommendation, *, ceiling_rps: Optional[float]) -> dict:
    """MEASUREMENT only -- two distinct numbers, never conflated:

        observed_nonfailing_offered_rps      no FAIL observed (may be INCONCLUSIVE)
        statistically_confirmed_offered_rps  strictly PASS at the configured confidence

    A rate sweep deliberately goes above quota (to see throttling and
    burst behavior), and a short window can pass there on Bedrock's
    burst allowance -- measured_burst_ceiling_rps records that, but it
    is observed serving, not a sustainable rate. The policy step
    (headroom + quota cap -> sustained_rps) is the entry's
    `recommendation` block (recommendation.py), never here.
    """
    saturation_rps = rec.saturation_point.rps if rec.saturation_point is not None else None
    confirmed = rec.confirmed_point.rps if rec.confirmed_point is not None else None
    out = {
        "observed_nonfailing_offered_rps": rec.point.rps,
        **_observed_fields(rec),
        "observed_slo_goodput_rps": rec.point.metrics.slo_goodput_rps,
        "statistically_confirmed_offered_rps": confirmed,
        "confirmation_source": rec.confirmation_source,  # confirmation | discovery_fixed_sequence
        "confirmed_slo_goodput_rps": rec.confirmed_point.metrics.slo_goodput_rps if rec.confirmed_point else None,
        # Highest swept rate that didn't FAIL anywhere in the sweep --
        # observed short-window serving, possibly above quota on burst.
        "measured_burst_ceiling_rps": rec.burst_point.rps if rec.burst_point is not None else None,
        "provider_ceiling_rps": round(ceiling_rps, 4) if ceiling_rps else None,
        "saturation_offered_rps": saturation_rps,
        **_saturation_fields(rec),
    }
    return out


def _sweep_points(profile_report) -> List[dict]:
    """Every swept point's verdict -- the transition region at a glance."""
    out = []
    for point, verdict in zip(profile_report.points, profile_report.verdicts):
        row = {"value": _value(point), "verdict": verdict.verdict, "phase": point.phase,
               "repetitions": len(point.repetitions) or 1, "n": point.metrics.n}
        failed = [c.name for c in verdict.checks if c.verdict == "FAIL"]
        if failed:
            row["failed"] = failed
        inconclusive = verdict.inconclusive_checks
        if inconclusive:
            row["inconclusive"] = [f"{c.name}: n={c.n} < required_n={c.required_n}" for c in inconclusive]
        out.append(row)
    return out


_STOP_HINTS = {
    "observed_violation": "the SLO was violated in fresh data there -- a lower candidate may confirm (candidates > 1)",
    "looks_exhausted": "bad events used up every planned look",
    "max_repetitions": "raise the confirmation caps to collect more samples",
    "max_requests": "raise the confirmation caps to collect more samples",
    "max_duration": "raise the confirmation caps to collect more samples",
    "unreachable_within_caps": "its next look can't be reached within the confirmation caps -- raise them",
}


_LATENCY_CHECKS = ("ttft_p95", "tpot_p95", "latency_p95")


def _summary(rec: Recommendation) -> str:
    """The three numbers side by side, so an INCONCLUSIVE observed point
    between capacity and saturation reads as 'not proven' -- never as
    'unsafe', and never as the capacity."""
    confirmed = _value(rec.confirmed_point) if rec.confirmed_point is not None else None
    observed = _value(rec.point)
    parts = [f"statistically_confirmed={confirmed if confirmed is not None else 'none'} (the capacity)"]
    if confirmed is None or observed != confirmed:
        note = {"INCONCLUSIVE": "INCONCLUSIVE -- no violation seen, too few requests to prove the SLO; not shown unsafe",
                "PASS": "PASS in discovery, not confirmed on independent data"}.get(rec.verdict.verdict, rec.verdict.verdict)
        parts.append(f"observed_nonfailing={observed} ({note})")
    sat = _value(rec.saturation_point) if rec.saturation_point is not None else None
    parts.append(f"saturation={sat} (first FAIL)" if sat is not None else f"saturation={rec.analysis.status}")
    return " | ".join(parts)


def _diagnosis(rec: Recommendation, profile_report, ceiling) -> dict:
    """What limits capacity -- read from the saturation point's FAILED
    checks, not guessed from one number:

      rpm_quota / tpm_quota   throttling (and only throttle-caused
                              failures), latency still within SLO
      latency                 a latency check failed, no throttling
      quota_and_latency       both
      errors                  non-throttle failures
      not_reached / unresolved  no clean saturation point to read
    """
    latency_at_observed = {
        c.name: {"observed": c.observed, "threshold": c.threshold, "verdict": c.verdict}
        for c in rec.verdict.checks if c.name in _LATENCY_CHECKS
    }
    out: dict = {"latency_at_observed_nonfailing": latency_at_observed,
                 # None when no latency check is configured -- not vacuously healthy.
                 "latency_healthy_at_observed_nonfailing": (
                     all(c["verdict"] != FAIL for c in latency_at_observed.values()) if latency_at_observed else None)}
    sat = rec.saturation_point
    if sat is None:
        out["bottleneck"] = rec.analysis.status  # not_reached | unresolved
        return out
    verdict = next((v for p, v in zip(profile_report.points, profile_report.verdicts) if p is sat), None)
    failed = sorted({c.name.split(".")[-1] for c in (verdict.checks if verdict else []) if c.verdict == FAIL})
    m = sat.metrics
    other_errors = max(0.0, round(1.0 - m.success_rate - m.throttle_rate, 4))
    throttled = "throttle_rate" in failed or ("success_rate" in failed and m.throttle_rate > 0 and other_errors == 0)
    slow = any(name in _LATENCY_CHECKS for name in failed)
    if throttled and slow:
        bottleneck = "quota_and_latency"
    elif throttled:
        bottleneck = f"{ceiling.binding}_quota" if ceiling is not None else "provider_throttling"
    elif slow:
        bottleneck = "latency"
    else:
        bottleneck = "errors"
    attempted = round(m.n / m.measured_duration_s, 4) if m.measured_duration_s else None
    out.update({
        "bottleneck": bottleneck,
        "saturation_at": _value(sat),
        "failed_checks": failed,
        "throttle_rate_at_saturation": m.throttle_rate,
        "non_throttle_error_rate_at_saturation": other_errors,
        "attempted_rps_at_saturation": attempted,       # requests sent per second
        "served_rps_at_saturation": m.request_throughput_rps,  # successes completed per second
        "provider_ceiling_rps": round(ceiling.rps, 4) if ceiling is not None and ceiling.rps else None,
    })
    return out


def _characterization() -> dict:
    return {"admission_envelope": None,
            "reason": "characterization experiment -- measures how token shape / context move the envelope; "
                      "production admission envelopes come only from reference experiments"}


def _unconfirmed_reason(profile_report, spec) -> str:
    """WHY nothing was statistically confirmed, from what actually ran --
    a confirmation phase's stop reason, or (discovery only) the point
    where the fixed-sequence test stopped and how many requests it
    lacked. Never suggests relaxing the SLO."""
    prefix = "no statistically confirmed point -- "
    if profile_report.recommendation is None:
        return prefix + "the first swept value already FAILs the SLO, so there is nothing to confirm; sweep lower values"
    if profile_report.confirmation_plan is not None:
        tried = sorted(profile_report.confirmations, key=lambda c: c.value)
        failed = next((c for c in tried if c.verdict != PASS), None)
        if failed is None:
            where = " at or below the provider ceiling" if spec.sweep.type == "rate" else ""
            return prefix + f"no non-failing discovery point{where} to confirm"
        text = f"confirmation at {failed.value:g}: {failed.verdict} ({failed.stop_reason}, n={failed.n}"
        if failed.next_look_n is not None:
            text += f", next look at n={failed.next_look_n}"
        text += ")"
        hint = _STOP_HINTS.get(failed.stop_reason)
        return prefix + text + (f"; {hint}" if hint else "") + " -- see `confirmation.candidates`"
    ordered = sorted(zip(profile_report.points, profile_report.verdicts), key=lambda pv: _value(pv[0]))
    stop = next(((p, v) for p, v in ordered if v.verdict != PASS), None)
    text = prefix + ("discovery only (no `confirmation:` phase), a fixed-sequence test that stops at the first "
                     "non-PASS point")
    if stop is None:
        return text
    point, verdict = stop
    text += f": {_value(point):g} is {verdict.verdict}"
    lacking = [f"{c.name} n={c.n} < required_n={c.required_n}" for c in verdict.inconclusive_checks]
    if verdict.verdict == INCONCLUSIVE and lacking:
        text += f" ({'; '.join(lacking)}) -- add a `confirmation:` block to the experiment to collect them"
    return text


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
        "ttft_p95_ms": m.ttft_p95_ms,
        "tpot_p95_ms": m.tpot_p95_ms,
        "latency_p95_ms": m.latency_p95_ms,
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
        "tpot_p95_ms": slo.tpot_p95_ms,
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
    if profile_report.verdicts:
        # Discovery only: picks candidates and shows the transition region.
        entry["sweep_points"] = _sweep_points(profile_report)
    swept = {_value(p) for p in points}
    skipped = [v for v in spec.sweep_values(subject) if v not in swept]
    if skipped and spec.sweep.stop_after_fails is not None:
        entry["sweep_stopped_early"] = {"after_consecutive_fails": spec.sweep.stop_after_fails, "skipped_values": skipped}
    if profile_report.confirmation_plan is not None:
        # Independent data at the candidates; the only source of
        # statistically_confirmed when present.
        entry["confirmation"] = {
            "plan": profile_report.confirmation_plan.to_dict(),
            "candidates": [c.to_dict() for c in profile_report.confirmations],
        }
    rec = profile_report.recommendation
    if rec is None:
        analysis = profile_report.analysis
        if analysis is not None and analysis.status == "unresolved":
            entry["note"] = ("non-monotonic from the first swept value -- no stable passing region; "
                             "re-run with repetitions")
            entry["unstable_region"] = analysis.unstable_region
        else:
            entry["note"] = "no swept value met the configured SLO -- re-run with lower sweep values"
        entry["recommendation"] = _characterization() if spec.purpose != "reference" else admission_envelope(
            spec.sweep.type, None, headroom=spec.provider_headroom,
            unconfirmed_reason=_unconfirmed_reason(profile_report, spec),
        )
        return
    ceiling = spec.provider_ceilings.get(subject)
    ceiling_rps = ceiling.rps if ceiling else None
    if spec.sweep.type == "concurrency":
        entry["concurrency"] = _concurrency_block(rec)
        confirmed = rec.confirmed_point.concurrency if rec.confirmed_point is not None else None
    else:
        entry["rate"] = _rate_block(rec, ceiling_rps=ceiling_rps)
        confirmed = rec.confirmed_point.rps if rec.confirmed_point is not None else None
    block = entry["concurrency" if spec.sweep.type == "concurrency" else "rate"]
    # isolated_workload_class: this class running ALONE -- per-class
    # values are not additive across classes and are not a global limit.
    scope = "workload_mix" if profile_report.mix_shares is not None else "isolated_workload_class"
    block["scope"] = scope
    block["summary"] = _summary(rec)
    # MEASUREMENT interpretation: what limits this envelope.
    entry["diagnosis"] = _diagnosis(rec, profile_report, ceiling)
    # POLICY, kept apart from the measurement above: the confirmed point
    # after this benchmark's safety headroom (recommendation.py) -- only
    # from a reference experiment.
    entry["recommendation"] = _characterization() if spec.purpose != "reference" else admission_envelope(
        spec.sweep.type, confirmed, headroom=spec.provider_headroom, quota_headroom=spec.quota_headroom,
        provider_ceiling_rps=ceiling_rps, scope=scope,
        unconfirmed_reason=None if confirmed is not None else _unconfirmed_reason(profile_report, spec),
    )
    # evidence = the observed point; confirmed_evidence = the point the
    # recommendation is derived from, when it's a different point.
    entry["evidence"] = _evidence(rec.point)
    entry["evidence"]["verdict"] = rec.verdict.to_dict()
    if rec.confirmed_point is not None and rec.confirmed_point is not rec.point:
        entry["confirmed_evidence"] = _evidence(rec.confirmed_point)


_REPO_ROOT = Path(__file__).resolve().parents[2]


def _git(*args: str) -> Optional[str]:
    try:
        out = subprocess.run(["git", *args], cwd=_REPO_ROOT, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


# The code that RUNS is the code imported when the process started -- a
# commit made during a long run must not be attributed to it. Captured
# once, at import.
_GIT_AT_START = (_git("rev-parse", "HEAD"), _git("status", "--porcelain", "--untracked-files=no"))


def _version(dist: str) -> Optional[str]:
    try:
        return metadata.version(dist)
    except metadata.PackageNotFoundError:
        return None


def _iso(ts: Optional[float]) -> Optional[str]:
    return None if ts is None else datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def _environment(report: ExperimentReport) -> dict:
    """Provenance: the measured envelope belongs to THIS environment at
    THIS time -- model + Bedrock serving + inference-profile routing +
    account/region quota + provider conditions then. Needed to compare
    runs over time (scripts/drift.py) and to know when a profile is stale."""
    spec = report.spec
    started = [r.started_at for r in report.all_results if r.started_at]
    completed = [r.completed_at for r in report.all_results if r.completed_at]
    commit, status = _GIT_AT_START
    return {
        "measured_at": {"start": _iso(min(started) if started else None),
                        "end": _iso(max(completed) if completed else None)},
        "account": spec.quota_account,
        "region": spec.target.region,
        "inference_profile": spec.target.model_id,
        "benchmark_version": _version("bedrock-runtime-benchmark"),
        "git_commit": commit,
        "git_dirty": None if status is None else bool(status),
        "runtime": {"python": platform.python_version(), "boto3": _version("boto3"),
                    "botocore": _version("botocore")},
    }


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
            "slo_profile": workload.slo_profile,
            "role": workload.role,  # reference | characterization (catalog/workloads.yaml)
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
        if rec is not None and rec.confirmed_point is not None:
            # Per class at the CONFIRMED point -- the capacity -- not the
            # observed one.
            entry["classes_at_confirmed_point"] = {
                name: {
                    "n": m.n, "slo_goodput_rps": m.slo_goodput_rps, "throttle_rate": m.throttle_rate,
                    "ttft_p95_ms": m.ttft_p95_ms, "tpot_p95_ms": m.tpot_p95_ms, "latency_p95_ms": m.latency_p95_ms,
                }
                for name, m in rec.confirmed_point.class_metrics.items()
            }
        mixed[profile_report.workload_name] = entry

    confidence = spec.slo.confidence or DEFAULT_CONFIDENCE
    return {
        "schema_version": 13,
        "experiment": spec.name,
        # reference: carries production admission envelopes;
        # characterization: measurement only (recommendation always null).
        "purpose": spec.purpose,
        "environment": _environment(report),
        # One profile is ONE snapshot of provider conditions. Validity
        # across time comes from comparing repeated runs (scripts/drift.py),
        # which reports runs / days_observed / spread per envelope.
        "validity": {
            # One run is never more than this; a stable / conservative
            # envelope needs repeated independent runs (scripts/drift.py).
            "envelope": "single_run_operating_envelope",
            "repeated_runs": 1,
            "days_observed": 1,
            "scope": "single run -- a snapshot of the provider conditions at measured_at; re-measure on "
                     "other days and times and run scripts/drift.py for a temporal_validation",
        },
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
                "account": spec.quota_account,
                "region": spec.target.region,
                "rpm": spec.quota_snapshot.rpm,
                "tpm": spec.quota_snapshot.tpm,
                "output_burndown": spec.output_burndown,
            },
            # The profiles this experiment's workloads use (each class
            # names its own under workload_classes.<name>.slo_profile).
            # POLICY, not a result: externally supplied requirements the
            # envelope is judged against -- never derived from measurements.
            "slo": {
                "role": "policy_input",
                "profiles": {
                    n: _slo_dict(spec.slo_profiles[n])
                    for n in sorted({w.slo_profile for w in spec.workloads if w.slo_profile in spec.slo_profiles})
                },
            },
            # The catalog entries (catalog/workloads.yaml) as run: shape,
            # profile, and the workload-level E2E cap -- profiles carry
            # only TTFT/TPOT, so latency_p95_ms is null there and the
            # effective E2E limit is the one here.
            "workloads": {
                w.name: {"input_tokens": w.input_tokens, "output_tokens": w.output_tokens,
                         "slo_profile": w.slo_profile, "latency_p95_ms": w.latency_p95_ms, "role": w.role}
                for w in spec.workloads
            },
        },
        "measurement": {
            "warmup_s": spec.warmup_s,
            "window_s": spec.duration_s,
            "throttle_pause_s": spec.throttle_pause_s,  # concurrency sweeps: a worker's wait after a 429
            "repetitions": spec.repetitions,
            # rates/percentiles over requests scheduled in the window
            # (drain included); throughput over completions in it.
            "window_policy": "scheduled_in_window_for_rates__completed_in_window_for_throughput",
            "clock": "monotonic_durations__wall_clock_timestamps",
            # Every check is PASS / FAIL / INCONCLUSIVE; success & throttle
            # use exact Clopper-Pearson bounds at this confidence (observed violation ->
            # FAIL, bound clears -> PASS, otherwise INCONCLUSIVE).
            "gate": "pass_fail_inconclusive",
            "confidence": confidence,
            "confirmation": (
                {"max_looks": spec.confirmation.max_looks, "max_repetitions": spec.confirmation.max_repetitions,
                 "max_requests": spec.confirmation.max_requests, "max_duration_s": spec.confirmation.max_duration_s,
                 "candidates": spec.confirmation.candidates, "cooldown_s": spec.confirmation.cooldown_s}
                if spec.confirmation is not None else None
            ),
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
        # The policy applied to turn confirmed measurements into each
        # entry's `recommendation` -- configured
        # (constraints/recommendation-policy.yaml), not measured.
        "recommendation_policy": {
            "headroom_fraction": spec.provider_headroom,        # back-off from the confirmed point
            "quota_headroom_fraction": spec.quota_headroom,     # back-off from the provider ceiling
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
