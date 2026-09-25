"""Run a batch of experiment YAMLs back to back -- the engine behind
scripts/run_all.py.

Strictly sequential, never parallel: experiments on the same account
and region share the same Bedrock RPM/TPM quota, so running two at once
would make each measure the other's load as throttling.

One failed experiment (bad credentials, model access, a crash) doesn't
stop the batch unless fail_fast is set; the summary records it and the
batch exits non-zero. All artifacts land in one batch directory, with
a summary.yaml alongside, and optionally a gateway diff over every
produced capacity profile.
"""
from __future__ import annotations

import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import yaml

from .experiments.schema import load_experiment
from .gateway_diff import GatewayDiff, diff
from .run_file import TargetFactory, estimated_duration_s, recommendation_summary, run_file


@dataclass
class PlannedExperiment:
    path: str
    name: str
    model_id: str
    sweep: str
    estimated_s: float


@dataclass
class ExperimentResult:
    path: str
    name: str
    status: str  # "ok" | "failed" | "skipped"
    elapsed_s: float = 0.0
    recommendations: List[str] = field(default_factory=list)
    profile_path: Optional[str] = None
    jsonl_path: Optional[str] = None
    error: Optional[str] = None


@dataclass
class BatchResult:
    results_dir: Path
    results: List[ExperimentResult] = field(default_factory=list)
    gateway_diff: Optional[GatewayDiff] = None

    @property
    def failed(self) -> List[ExperimentResult]:
        return [r for r in self.results if r.status == "failed"]

    @property
    def gateway_warnings(self) -> int:
        return self.gateway_diff.to_dict()["summary"]["warn"] if self.gateway_diff else 0

    @property
    def exit_code(self) -> int:
        return 1 if self.failed or self.gateway_warnings else 0

    def to_dict(self) -> dict:
        out = {
            "results_dir": str(self.results_dir),
            "experiments": [
                {k: v for k, v in r.__dict__.items() if v not in (None, [])} for r in self.results
            ],
        }
        if self.gateway_diff is not None:
            out["gateway_diff"] = self.gateway_diff.to_dict()
        return out


def plan(paths: List[str]) -> List[PlannedExperiment]:
    """Loads (and so validates) every experiment before any runs -- a
    typo in the 5th file should fail in the first second, not after an
    hour of real Bedrock calls."""
    out = []
    for path in paths:
        spec = load_experiment(path)
        out.append(PlannedExperiment(
            path=path, name=spec.name, model_id=spec.target.model_id,
            sweep=f"{spec.sweep.type} {spec.sweep.values}", estimated_s=estimated_duration_s(spec),
        ))
    return out


def format_plan(planned: List[PlannedExperiment]) -> str:
    lines = [f"{'#':<3} {'experiment':<32} {'model':<40} {'est.':>7}"]
    for i, p in enumerate(planned, 1):
        lines.append(f"{i:<3} {p.name:<32} {p.model_id:<40} {p.estimated_s / 60:>5.0f}m")
    total = sum(p.estimated_s for p in planned)
    lines.append(f"total: {len(planned)} experiments, >= {total / 60:.0f} min (plus drain time), run sequentially")
    return "\n".join(lines)


def run_batch(
    paths: List[str], *, results_dir: Path, fail_fast: bool = False,
    gateway_config: Optional[dict] = None, target_factory: Optional[TargetFactory] = None,
) -> BatchResult:
    batch = BatchResult(results_dir=results_dir)
    planned = plan(paths)
    total = len(planned)

    for i, p in enumerate(planned, 1):
        if fail_fast and batch.failed:
            batch.results.append(ExperimentResult(path=p.path, name=p.name, status="skipped"))
            continue
        print(f"\n{'=' * 78}\n[{i}/{total}] {p.name}  ({p.path}, est. {p.estimated_s / 60:.0f}m)\n{'=' * 78}")
        start = time.perf_counter()
        try:
            outcome = run_file(p.path, results_dir=str(results_dir), target_factory=target_factory)
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # noqa: BLE001 - one broken experiment shouldn't sink the batch
            traceback.print_exc()
            batch.results.append(ExperimentResult(
                path=p.path, name=p.name, status="failed", elapsed_s=round(time.perf_counter() - start, 1),
                error=f"{type(exc).__name__}: {exc}",
            ))
            continue
        batch.results.append(ExperimentResult(
            path=p.path, name=p.name, status="ok", elapsed_s=round(outcome.elapsed_s, 1),
            recommendations=recommendation_summary(outcome.report),
            profile_path=str(outcome.profile_path), jsonl_path=str(outcome.jsonl_path),
        ))

    if gateway_config is not None:
        profiles = [yaml.safe_load(Path(r.profile_path).read_text()) for r in batch.results if r.profile_path]
        if profiles:
            batch.gateway_diff = diff(profiles, gateway_config)

    results_dir.mkdir(parents=True, exist_ok=True)
    (results_dir / "summary.yaml").write_text(yaml.safe_dump(batch.to_dict(), sort_keys=False, width=120))
    return batch


def format_summary(batch: BatchResult) -> str:
    lines = [f"{'experiment':<32} {'status':<8} {'time':>7}  recommendation"]
    for r in batch.results:
        first, *rest = r.recommendations or [r.error or ""]
        lines.append(f"{r.name:<32} {r.status:<8} {r.elapsed_s / 60:>6.1f}m  {first}")
        lines.extend(f"{'':<50}{line}" for line in rest)
    ok = sum(1 for r in batch.results if r.status == "ok")
    lines.append(f"\n{ok}/{len(batch.results)} succeeded; artifacts + summary.yaml in {batch.results_dir}")
    if batch.gateway_diff is not None:
        s = batch.gateway_diff.to_dict()["summary"]
        lines.append(f"gateway diff: {s['warn']} warn, {s['info']} info, {s['proposed_changes']} proposed changes "
                     f"(details in summary.yaml)")
    return "\n".join(lines)
