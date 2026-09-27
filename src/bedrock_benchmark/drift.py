"""Temporal validation -- turning single-run snapshots into a stable,
conservative operating envelope.

One capacity profile is a SINGLE-RUN OPERATING ENVELOPE: one snapshot of
provider conditions (model + Bedrock serving + routing + quota + the
conditions at measured_at). The same envelope measured this morning,
tonight and next week can differ. This module lines repeated profiles up
per (model, experiment, workload/mix, sweep kind) and states, as a
`temporal_validation` block, what the runs support together:

  runs / days_observed / utc_hours_observed    how much evidence, over what time
  confirmed_runs / unconfirmed_runs            runs that did / didn't confirm a point
  confirmed_<unit>                             min / median / max / spread_pct
  conservative_<unit>                          the MINIMUM confirmed value
  conservative_admission                       the MINIMUM admission-envelope value
  envelope                                     what the evidence supports:
      single_run_operating_envelope      one run -- a snapshot, nothing about time
      insufficient_temporal_evidence     fewer than min_runs runs or min_days days
      stable_operating_envelope          every run confirmed, spread <= threshold
      unstable_operating_envelope        otherwise -- use the conservative value

This needs several INDEPENDENT runs at different times, so it is never
produced by a single run_all.py invocation; scripts/drift.py computes it
over whatever profiles exist. A profile without `environment.measured_at`
(schema < 10) still counts as a run, but not toward days/hours observed.
"""
from __future__ import annotations

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
            out.append((str(f), yaml.safe_load(f.read_text()) or {}))
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
                groups.setdefault((model, profile.get("experiment"), scope, name, kind), []).append({
                    "measured_at": measured, "confirmed": confirmed, "admission": production,
                    "git_commit": env.get("git_commit"), "profile": path,
                })

    report = []
    for (model, experiment, scope, name, kind), runs in sorted(groups.items(), key=lambda kv: tuple(map(str, kv[0]))):
        runs.sort(key=lambda r: r["measured_at"] or "")
        unit, admission_key = _UNITS[kind]
        confirmed = [r["confirmed"] for r in runs if r["confirmed"] is not None]
        admission = [r["admission"] for r in runs if r["admission"] is not None]
        stamps = [r["measured_at"] for r in runs if r["measured_at"]]
        days = {t[:10] for t in stamps}
        tv: dict = {
            "runs": len(runs), "days_observed": len(days),
            "utc_hours_observed": sorted({int(t[11:13]) for t in stamps}),
            "confirmed_runs": len(confirmed), "unconfirmed_runs": len(runs) - len(confirmed),
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
        tv["criteria"] = {"min_runs": min_runs, "min_days": min_days, "max_spread_pct": stability_threshold_pct}
        report.append({
            "model": model, "experiment": experiment, scope: name, "kind": kind,
            "temporal_validation": tv,
            "runs": [{k: v for k, v in r.items() if v is not None} for r in runs],
        })
    return report
