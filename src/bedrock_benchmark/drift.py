"""Temporal validation -- turning single-run snapshots into a stable,
conservative operating envelope.

One capacity profile is a SINGLE-RUN OPERATING ENVELOPE: one snapshot of
provider conditions (model + Bedrock serving + routing + quota + the
conditions at measured_at). The same envelope measured this morning,
tonight and next week can differ. This module lines repeated profiles up
per model, experiment, workload/mix, sweep kind, mode, candidate and
measurement duration, and states, as a
`temporal_validation` block, what the runs support together:

  runs / days_observed / utc_hours_observed    how much evidence, over what time
  confirmed_runs / unconfirmed_runs            runs that did / didn't confirm a point
  confirmed_<unit>                             min / median / max / spread_pct
  conservative_<unit>                          the MINIMUM confirmed value
  conservative_admission                       the MINIMUM admission-envelope value
  production_capacity_input                    conservative_admission once min_runs /
                                               min_days are met -- else None: a single
                                               run is never a production-safe config
  envelope                                     what the evidence supports:
      single_run_operating_envelope      one run -- a snapshot, nothing about time
      insufficient_temporal_evidence     fewer than min_runs runs or min_days days
      stable_operating_envelope          every run confirmed, spread <= threshold
      unstable_operating_envelope        otherwise -- use the conservative value

This needs several INDEPENDENT runs at different times, so it is never
produced by a single run; `bedrock-benchmark validate` computes it over
whatever profiles exist and writes a temporal-capacity-profile.yaml
(build_temporal_profile). Runs whose measurement_validity is invalid are
excluded. A profile without `environment.measured_at`
(schema < 10) still counts as a run, but not toward days/hours observed.
"""
from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import yaml


def _profiles(paths: Iterable[str]) -> List[Tuple[str, dict]]:
    out = []
    for raw in paths:
        p = Path(raw)
        files = sorted(p.rglob("*-capacity-profile.yaml")) if p.is_dir() else [p]
        for f in files:
            doc = yaml.safe_load(f.read_text()) or {}
            if doc.get("artifact") == "temporal_capacity_profile":
                continue  # a previous `validate` output, not a run
            out.append((str(f), doc))
    return out


def _envelope(kind: str, block: dict) -> Tuple[Optional[float], Optional[float]]:
    """(statistically confirmed value, production value) across schema versions."""
    if kind == "rate":
        confirmed = block.get("statistically_confirmed_offered_rps",
                              block.get("measured_safe_offered_rps", block.get("max_safe_offered_rps")))
        production = block.get("production_sustained_rps", block.get("production_offered_rps"))
    else:
        confirmed = block.get("statistically_confirmed", block.get("measured_best"))
        production = block.get("production_max")
    return confirmed, production


_UNITS = {"rate": ("rps", "sustained_rps"), "concurrency": ("concurrency", "max_inflight")}


def _envelope_type(n_runs: int, n_days: int, all_confirmed: bool, spread: Optional[float], *,
                   threshold: float, min_runs: int, min_days: int) -> str:
    if n_runs <= 1:
        return "single_run_operating_envelope"
    if n_runs < min_runs or n_days < min_days:
        return "insufficient_temporal_evidence"
    if all_confirmed and spread is not None and spread <= threshold:
        return "stable_operating_envelope"
    return "unstable_operating_envelope"


def summarize(paths: Iterable[str], *, stability_threshold_pct: float = 20.0, min_runs: int = 3,
              min_days: int = 2) -> List[dict]:
    groups: Dict[tuple, List[dict]] = {}
    for path, profile in _profiles(paths):
        env = profile.get("environment") or {}
        measured = (env.get("measured_at") or {}).get("start")
        model = (profile.get("model") or {}).get("name") or (profile.get("model") or {}).get("model_id")
        measurement = profile.get("measurement") or {}
        retest = measurement.get("retest") or {}
        # Historical filenames/experiment names stay untouched and are not
        # automatically pooled with renamed experiments.
        legacy_sustain = str(profile.get("experiment", "")).endswith("-sustain")
        mode = profile.get("mode") or ("sustain" if retest or legacy_sustain else
                                       "history" if measurement.get("history_protocol") else "sweep")
        comparison = {
            "mode": mode,
            "window_s": measurement.get("window_s"),
            "min_steady_state_duration_s": (measurement.get("confirmation") or {}).get("min_steady_state_duration_s"),
            "candidate_concurrency": (retest.get("concurrency") if retest else
                                      (profile.get("sweep") or {}).get("values")) if mode == "sustain" else None,
            "workload": retest.get("workload"),
            "duration_s": retest.get("duration_s"),
            "slo_policy": (profile.get("constraints") or {}).get("slo"),
            "continuous_confirmation": (measurement.get("confirmation") or {}).get("continuous", False),
            "recovery_policy": measurement.get("isolation"),
        }
        comparison_key = json.dumps(comparison, sort_keys=True)
        subjects = [("class", n, e) for n, e in (profile.get("workload_classes") or {}).items()]
        subjects += [("mix", n, e) for n, e in (profile.get("mixed_workloads") or {}).items()]
        for scope, name, entry in subjects:
            for kind in ("rate", "concurrency"):
                if kind not in entry:
                    continue
                confirmed, production = _envelope(kind, entry[kind])
                if "recommendation" in entry:  # v11+: policy lives in the recommendation block
                    envelope = (entry["recommendation"] or {}).get("admission_envelope")
                    production = None if envelope is None else envelope.get(_UNITS[kind][1])
                groups.setdefault((model, profile.get("experiment"), scope, name, kind, comparison_key), []).append({
                    "measured_at": measured, "confirmed": confirmed, "admission": production,
                    "git_commit": env.get("git_commit"), "profile": path,
                    # v23+: a run whose provider never recovered supports no conclusion.
                    "measurement_validity": ("invalid" if
                        (entry.get("workload_validation") or {}).get("valid") is False
                        or any((profile.get("workload_classes", {}).get(n, {}).get("workload_validation") or {}).get("valid") is False
                               for n, share in (entry.get("shares") or {}).items() if share > 0)
                        else (entry.get("measurement_validity") or {}).get("status", "valid")),
                })

    report = []
    for (model, experiment, scope, name, kind, comparison_key), all_runs in sorted(groups.items(), key=lambda kv: tuple(map(str, kv[0]))):
        all_runs.sort(key=lambda r: r["measured_at"] or "")
        # Only VALID measurements are evidence: an invalid run (provider never
        # recovered) is listed but counts toward nothing -- not as a
        # confirmation, not as an unconfirmed run, not toward days.
        runs = [r for r in all_runs if r["measurement_validity"] != "invalid"]
        unit, admission_key = _UNITS[kind]
        confirmed = [r["confirmed"] for r in runs if r["confirmed"] is not None]
        admission = [r["admission"] for r in runs if r["admission"] is not None]
        stamps = [r["measured_at"] for r in runs if r["measured_at"]]
        days = {t[:10] for t in stamps}
        tv: dict = {
            "runs": len(runs), "days_observed": len(days),
            "utc_hours_observed": sorted({int(t[11:13]) for t in stamps}),
            "confirmed_runs": len(confirmed), "unconfirmed_runs": len(runs) - len(confirmed),
            "invalid_runs": len(all_runs) - len(runs),
        }
        spread = None
        if confirmed:
            med = statistics.median(confirmed)
            spread = round((max(confirmed) - min(confirmed)) / med * 100, 1) if med else None
            tv[f"confirmed_{unit}"] = {"min": min(confirmed), "median": med, "max": max(confirmed),
                                       "spread_pct": spread}
            tv[f"conservative_{unit}"] = min(confirmed)
        if admission:
            tv["conservative_admission"] = {admission_key: min(admission)}
        tv["envelope"] = _envelope_type(len(runs), len(days), len(confirmed) == len(runs), spread,
                                        threshold=stability_threshold_pct, min_runs=min_runs, min_days=min_days)
        # The ONLY value to feed production capacity: the conservative
        # (minimum) admission value, once there is enough temporal
        # evidence -- stable, or unstable (then the minimum is exactly the
        # point). None for a single run or insufficient evidence.
        tv["production_capacity_input"] = (
            tv.get("conservative_admission")
            if tv["envelope"] in ("stable_operating_envelope", "unstable_operating_envelope") else None)
        tv["criteria"] = {"min_runs": min_runs, "min_days": min_days, "max_spread_pct": stability_threshold_pct}
        comparison = json.loads(comparison_key)
        report.append({
            "model": model, "experiment": experiment, scope: name, "kind": kind,
            "mode": comparison["mode"], "comparison": comparison,
            "temporal_validation": tv,
            "runs": [{k: v for k, v in r.items() if v is not None} for r in all_runs],
        })
    return report


TEMPORAL_PROFILE_SCHEMA_VERSION = 1

# temporal_validation.envelope -> the artifact's status (what a consumer may do).
_STATUS = {
    "stable_operating_envelope": "VALID",                     # production_capacity_input usable
    "unstable_operating_envelope": "VALID_CONSERVATIVE",      # usable: it IS the minimum across runs
    "insufficient_temporal_evidence": "INSUFFICIENT_EVIDENCE",
    "single_run_operating_envelope": "INSUFFICIENT_EVIDENCE",
}


def build_temporal_profile(paths: Iterable[str], *, stability_threshold_pct: float = 20.0, min_runs: int = 3,
                           min_days: int = 2) -> dict:
    """The temporal-capacity-profile artifact: repeated single-run profiles
    -> per (model, experiment, class/mix, kind) status and the ONLY value
    meant for production capacity (production_capacity_input). A different
    artifact type from a capacity profile on purpose -- a single run never
    is one of these."""
    from datetime import datetime, timezone
    paths = list(paths)
    entries = summarize(paths, stability_threshold_pct=stability_threshold_pct, min_runs=min_runs,
                        min_days=min_days)
    for e in entries:
        e["status"] = _STATUS[e["temporal_validation"]["envelope"]]
    return {
        "artifact": "temporal_capacity_profile",
        "temporal_profile_schema_version": TEMPORAL_PROFILE_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "inputs": {"paths": paths, "profiles": len(_profiles(paths))},
        "criteria": {"min_runs": min_runs, "min_days": min_days, "max_spread_pct": stability_threshold_pct},
        "entries": entries,
    }


def format_temporal(profile: dict) -> str:
    lines = [f"TEMPORAL VALIDATION  ({profile['inputs']['profiles']} profiles; needs >= "
             f"{profile['criteria']['min_runs']} runs across >= {profile['criteria']['min_days']} days)"]
    for e in profile["entries"]:
        tv = e["temporal_validation"]
        name = e.get("class") or e.get("mix")
        use = tv.get("production_capacity_input")
        details = ", ".join(f"{key}={value}" for key, value in (e.get("comparison") or {}).items()
                            if value is not None and key not in ("mode", "workload", "slo_policy", "recovery_policy"))
        lines.append(f"  {e['model']} / {e['experiment']} / {name} [{e['kind']}; mode={e.get('mode', 'sweep')}]: {e['status']}  "
                     f"runs={tv['runs']} days={tv['days_observed']} invalid={tv.get('invalid_runs', 0)}  "
                     f"production_capacity_input={use if use else 'none'}")
        if details:
            lines.append(f"    {details}")
    return "\n".join(lines)
