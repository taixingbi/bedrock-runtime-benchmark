"""Drift across repeated runs -- one capacity profile is ONE snapshot of
provider conditions (model + Bedrock serving + routing + quota + the
conditions at measured_at). The same envelope measured today, tomorrow
and tonight can differ; this module lines repeated profiles up per
(model, experiment, workload/mix, sweep kind) and reports how stable the
statistically confirmed envelope actually is:

  repeated_runs / days_observed   how much evidence there is over time
  confirmed_runs                  runs that confirmed any point at all
  confirmed / production values   per run, oldest first, with min/median/max
  spread_pct                      (max - min) / median of confirmed values
  stable                          spread_pct <= the threshold (default 20%)
  conservative_production         the MINIMUM production value across runs
                                  (v11+: recommendation.admission_envelope)

A profile without `environment.measured_at` (schema < 10) still counts as
a run, but not toward days_observed.
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


def summarize(paths: Iterable[str], *, stability_threshold_pct: float = 20.0) -> List[dict]:
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
                    production = None if envelope is None else envelope.get(
                        "sustained_rps" if kind == "rate" else "max_inflight")
                groups.setdefault((model, profile.get("experiment"), scope, name, kind), []).append({
                    "measured_at": measured, "confirmed": confirmed, "production": production,
                    "git_commit": env.get("git_commit"), "profile": path,
                })

    report = []
    for (model, experiment, scope, name, kind), runs in sorted(groups.items(), key=lambda kv: tuple(map(str, kv[0]))):
        runs.sort(key=lambda r: r["measured_at"] or "")
        confirmed = [r["confirmed"] for r in runs if r["confirmed"] is not None]
        production = [r["production"] for r in runs if r["production"] is not None]
        days = {r["measured_at"][:10] for r in runs if r["measured_at"]}
        entry = {
            "model": model, "experiment": experiment, scope: name, "kind": kind,
            "repeated_runs": len(runs), "days_observed": len(days),
            "confirmed_runs": len(confirmed),
            "runs": [{k: v for k, v in r.items() if v is not None} for r in runs],
        }
        if confirmed:
            med = statistics.median(confirmed)
            spread = round((max(confirmed) - min(confirmed)) / med * 100, 1) if med else None
            entry["confirmed"] = {"min": min(confirmed), "median": med, "max": max(confirmed), "spread_pct": spread}
            entry["stable"] = (len(confirmed) >= 2 and spread is not None and spread <= stability_threshold_pct)
        if production:
            entry["conservative_production"] = min(production)
        if len(runs) < 2 or len(days) < 2:
            entry["note"] = "fewer than 2 runs / days -- no evidence about stability over time yet"
        report.append(entry)
    return report
