"""Run every (model x experiment) pair back to back -- the engine behind
scripts/run.py and scripts/run_all.py.

Models come from the models file (catalog/models.yaml); experiments are
model-agnostic, so the batch is the cross product, grouped by model:

    results/<batch>/
      nova-micro/  concurrency-sweep-<id>.jsonl, ...-capacity-profile.yaml, ...
      nova-pro/    ...
      summary.yaml

Strictly sequential, never parallel: runs against the same model share
its Bedrock RPM/TPM quota, so two at once would make each measure the
other's load as throttling.

Every pair is bound and validated before the first Bedrock call. One
failed run (bad credentials, no model access, a crash) doesn't stop the
batch unless fail_fast is set; the summary records it and the batch
exits non-zero. Optionally runs a gateway diff over every produced
capacity profile.
"""
from __future__ import annotations

import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Collection, List, Optional

import yaml

from .constraints import DEFAULT_SLO_FILE
from .experiments.schema import NoMatchingWorkloads, load_experiment
from .gateway_diff import GatewayDiff, diff
from .models import ModelConfig
from .workload import DEFAULT_WORKLOADS_FILE
from .run_file import TargetFactory, describe_sweep, estimated_duration_s, recommendation_summary, run_file


@dataclass
class PlannedRun:
    path: str
    experiment: str
    model: ModelConfig
    sweep: str
    estimated_s: float
    # Set when an --slo-profile filter leaves this pair nothing to run.
    skip_reason: Optional[str] = None


@dataclass
class RunResult:
    model: str
    experiment: str
    path: str
    status: str  # "ok" | "failed" | "skipped"
    elapsed_s: float = 0.0
    recommendations: List[str] = field(default_factory=list)
    profile_path: Optional[str] = None
    jsonl_path: Optional[str] = None
    error: Optional[str] = None


@dataclass
class BatchResult:
    results_dir: Path
    results: List[RunResult] = field(default_factory=list)
    gateway_diff: Optional[GatewayDiff] = None

    @property
    def failed(self) -> List[RunResult]:
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
            "runs": [{k: v for k, v in r.__dict__.items() if v not in (None, [])} for r in self.results],
        }
        if self.gateway_diff is not None:
            out["gateway_diff"] = self.gateway_diff.to_dict()
        return out


def plan(
    paths: List[str], models: List[ModelConfig], *, slo_file: str = DEFAULT_SLO_FILE,
    workloads_file: str = DEFAULT_WORKLOADS_FILE, only_slo_profiles: Optional[Collection[str]] = None,
) -> List[PlannedRun]:
    """Binds (and so validates) every pair before any runs -- a typo in
    one file, or a model missing the quota a rate sweep needs, fails in
    the first second, not after an hour of real Bedrock calls."""
    out = []
    for model in models:
        for path in paths:
            try:
                spec = load_experiment(path, model, slo_file=slo_file, workloads_file=workloads_file,
                                       only_slo_profiles=only_slo_profiles)
            except NoMatchingWorkloads as skip:
                out.append(PlannedRun(path=path, experiment=Path(path).stem, model=model, sweep="",
                                      estimated_s=0.0, skip_reason=str(skip)))
                continue
            out.append(PlannedRun(
                path=path, experiment=spec.name, model=model,
                sweep=describe_sweep(spec), estimated_s=estimated_duration_s(spec),
            ))
    return out


def format_plan(planned: List[PlannedRun]) -> str:
    runs = [p for p in planned if p.skip_reason is None]
    lines = [f"{'#':<3} {'model':<14} {'experiment':<20} {'est.':>5}  sweep"]
    for i, p in enumerate(runs, 1):
        lines.append(f"{i:<3} {p.model.name:<14} {p.experiment:<20} {p.estimated_s / 60:>4.0f}m  {p.sweep}")
    for p in planned:
        if p.skip_reason is not None:
            lines.append(f"--  {p.model.name:<14} {p.experiment:<20} skip  {p.skip_reason}")
    total = sum(p.estimated_s for p in runs)
    n_models = len({p.model.name for p in runs})
    lines.append(
        f"total: {len(runs)} runs ({n_models} models), >= {total / 60:.0f} min (plus drain time), run sequentially"
    )
    return "\n".join(lines)


def run_batch(
    paths: List[str], models: List[ModelConfig], *, results_dir: Path, fail_fast: bool = False,
    gateway_config: Optional[dict] = None, target_factory: Optional[TargetFactory] = None,
    slo_file: str = DEFAULT_SLO_FILE, workloads_file: str = DEFAULT_WORKLOADS_FILE,
    only_slo_profiles: Optional[Collection[str]] = None,
) -> BatchResult:
    batch = BatchResult(results_dir=results_dir)
    planned = [p for p in plan(paths, models, slo_file=slo_file, workloads_file=workloads_file,
                               only_slo_profiles=only_slo_profiles) if p.skip_reason is None]
    total = len(planned)

    for i, p in enumerate(planned, 1):
        base = dict(model=p.model.name, experiment=p.experiment, path=p.path)
        if fail_fast and batch.failed:
            batch.results.append(RunResult(**base, status="skipped"))
            continue
        print(f"\n{'=' * 78}\n[{i}/{total}] {p.model.name} / {p.experiment}  (est. {p.estimated_s / 60:.0f}m)\n{'=' * 78}")
        start = time.perf_counter()
        try:
            outcome = run_file(p.path, p.model, results_dir=str(results_dir), target_factory=target_factory,
                               slo_file=slo_file, workloads_file=workloads_file,
                               only_slo_profiles=only_slo_profiles)
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # noqa: BLE001 - one broken run shouldn't sink the batch
            traceback.print_exc()
            batch.results.append(RunResult(
                **base, status="failed", elapsed_s=round(time.perf_counter() - start, 1),
                error=f"{type(exc).__name__}: {exc}",
            ))
            continue
        batch.results.append(RunResult(
            **base, status="ok", elapsed_s=round(outcome.elapsed_s, 1),
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
    lines = [f"{'model':<14} {'experiment':<20} {'status':<8} {'time':>6}  recommendation"]
    for r in batch.results:
        first, *rest = r.recommendations or [r.error or ""]
        lines.append(f"{r.model:<14} {r.experiment:<20} {r.status:<8} {r.elapsed_s / 60:>5.1f}m  {first}")
        lines.extend(f"{'':<53}{line}" for line in rest)
    ok = sum(1 for r in batch.results if r.status == "ok")
    lines.append(f"\n{ok}/{len(batch.results)} succeeded; per-model folders + summary.yaml in {batch.results_dir}")
    if batch.gateway_diff is not None:
        s = batch.gateway_diff.to_dict()["summary"]
        lines.append(f"gateway diff: {s['warn']} warn, {s['info']} info, {s['proposed_changes']} proposed changes "
                     f"(details in summary.yaml)")
    return "\n".join(lines)
