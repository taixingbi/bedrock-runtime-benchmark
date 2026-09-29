"""Human summary of a capacity profile -- the three questions a user has:

    1. Can I trust this run?
    2. What did we learn?
    3. Can this number be used in production?

plus the next action. The full YAML stays the machine / researcher
interface; this is the human one.
"""
from __future__ import annotations

from typing import List, Optional

# Temporal-validation defaults a production input must meet (drift.py).
MIN_RUNS, MIN_DAYS = 3, 2


def _trust(entry: dict) -> (str, List[str]):
    issues = []
    validity = (entry.get("measurement_validity") or {}).get("status", "valid")
    if validity == "invalid":
        return "NO", ["provider never passed a recovery probe (measurement_validity: invalid)"]
    if validity == "suspect_reproduced":
        issues.append("throttling far below the quota ceiling reproduced after a verified recovery")
    if validity == "suspect_steady_state":
        issues.append("a confirmation look passed but later collected evidence violated the SLO")
    if validity == "suspect_non_monotonic":
        issues.append("discovery was non-monotonic; no single saturation edge is established")
    if (entry.get("workload_validation") or {}).get("valid") is False:
        issues.append("workload shape differs from its target (workload_validation)")
    if (entry.get("load_generator") or {}).get("valid") is False:
        issues.append("the load generator lagged (load_generator)")
    if entry.get("client_limited_points"):
        issues.append(f"client thread pool limited points {entry['client_limited_points']}")
    # Provider-state signs (also for profiles from before measurement_validity):
    # throttled while served far below the nominal ceiling.
    suspect = [p["value"] for p in entry.get("sweep_points") or []
               if (p.get("throttled_rps") or 0) > 0 and p.get("ceiling_ratio") is not None
               and p["ceiling_ratio"] < 0.5]
    suspect += [c["value"] for c in (entry.get("confirmation") or {}).get("candidates") or []
                if c.get("throttled_below_ceiling")]
    if suspect and validity == "valid" and "measurement_validity" not in entry:
        return "doubtful", [f"points {sorted(set(suspect))} throttled far below the quota ceiling -- provider "
                            f"state, not their own load (profile predates recovery probes)"]
    return ("yes" if not issues else "with caveats"), issues


def _block(entry: dict) -> (Optional[str], dict):
    for kind in ("concurrency", "rate"):
        if kind in entry:
            return kind, entry[kind]
    return None, {}


def summarize_entry(name: str, entry: dict, profile: dict) -> List[str]:
    if "history_comparison" in entry:
        arms = entry["history_comparison"]
        lines = [f"{name} / history comparison", "  Descriptive observations; no confirmed capacity or admission recommendation"]
        for arm in arms:
            m = arm.get("aggregate", {})
            lines.append(f"  {arm['scenario']} trial={arm['trial']} target={arm['target_rps']:.4g} rps: "
                         f"{arm['status']}, throttle={m.get('throttle_rate')}, successful_rps={m.get('successful_rps')}")
        return lines
    control = entry.get("role") == "reference_control"
    purpose = "characterization" if control else profile.get("purpose")
    kind, block = _block(entry)
    unit = "C" if kind == "concurrency" else "rps"
    confirmed = block.get("statistically_confirmed") if kind == "concurrency" \
        else block.get("statistically_confirmed_offered_rps")
    sat = block.get("saturation") or {}
    trust, issues = _trust(entry)
    lines = [f"{name} / {entry.get('slo_profile', 'mix')}"]
    lines.append(f"  Can I trust this run?      {trust}" + (f" -- {'; '.join(issues)}" if issues else ""))
    if kind is None:
        lines.append("  Observed                   no stable passing region (see sweep_points)")
    else:
        edge = sat.get("observed_edge")
        observed = block.get("observed_nonfailing", block.get("observed_nonfailing_offered_rps"))
        lines.append(f"  Observed                   non-failing up to {unit}={observed}; "
                     + (f"first FAIL at {unit}={edge}" if edge is not None else f"saturation {sat.get('status')}"))
    rec = entry.get("recommendation") or {}
    envelope = rec.get("admission_envelope")
    if confirmed is not None:
        lines.append(f"  Confirmed                  {unit}_safe = {confirmed}")
    else:
        why = (entry.get("calibration_point") or {}).get("reason") if purpose == "admission_calibration" else None
        candidates = (entry.get("confirmation") or {}).get("candidates") or []
        tried = [f"{c['value']}: {c['verdict']} ({c['stop_reason']})" for c in candidates
                 if c.get("stop_reason") != "not_tested"]
        detail = "; ".join(tried) if tried else (why or rec.get("reason") or "see sweep_points")
        lines.append(f"  Confirmed                  none -- {detail}")
    if control:
        lines.append("  Single-run recommendation  none -- reference control for interpreting the experiment")
    elif purpose == "admission_calibration":
        lines.append("  Single-run recommendation  none -- calibration evidence (calibration_point), not config")
    elif envelope:
        value = envelope.get("max_inflight") if envelope.get("max_inflight") is not None else envelope.get("sustained_rps")
        key = "max_inflight" if envelope.get("max_inflight") is not None else "sustained_rps"
        lines.append(f"  Single-run recommendation  {key} = {value}")
    else:
        lines.append("  Single-run recommendation  none")
    lines.append("  Production usable?         NO")
    if trust in ("NO", "doubtful"):
        lines.append("  Reason                     " + ("the measurement is invalid" if trust == "NO" else
                                                        "provider state likely contaminated the run")
                     + " -- no capacity conclusion from it")
        lines.append("  Next action                re-run once the provider / account is healthy "
                     "(bedrock-benchmark doctor first)")
    elif control:
        lines.append("  Reason                     explanatory reference control; no admission policy is derived")
        lines.append("  Next action                compare with the other workload shapes and provider-state evidence")
    elif confirmed is None:
        lines.append("  Reason                     nothing statistically confirmed")
        lines.append("  Next action                see Confirmed above; re-run (plan / pilot first)")
    elif purpose == "admission_calibration":
        lines.append("  Reason                     calibration evidence: derive a workload policy, validate it "
                     "under mixed traffic, then temporally")
        lines.append("  Next action                repeat on other days / times; bedrock-benchmark validate results/")
    else:
        runs = (profile.get("validity") or {}).get("repeated_runs", 1)
        days = (profile.get("validity") or {}).get("days_observed", 1)
        lines.append(f"  Reason                     only {runs} run / {days} day -- production input needs "
                     f"temporal validation (>= {MIN_RUNS} runs across >= {MIN_DAYS} days)")
        lines.append("  Next action                repeat this experiment on other days / times, then "
                     "bedrock-benchmark validate results/")
    return lines


def summarize_profile(profile: dict) -> str:
    run = profile.get("run") or {}
    model = (profile.get("model") or {}).get("name")
    head = [f"RESULT  {model} / {profile.get('experiment')}  [{profile.get('purpose')}]"
            + (f"  mode={profile['mode']}" if profile.get("mode") else "")
            + (f"  run {run['run_id']}" if run.get("run_id") else "")]
    body = []
    entries = {**(profile.get("workload_classes") or {}), **(profile.get("mixed_workloads") or {})}
    for name, entry in entries.items():
        if "recommendation" not in entry and "measurement_validity" not in entry:
            continue  # a class measured only inside a mix
        body.append("")
        body.extend(summarize_entry(name, entry, profile))
    return "\n".join(head + body)
