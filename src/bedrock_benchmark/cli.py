"""bedrock-benchmark -- the public command line.

    bedrock-benchmark list
    bedrock-benchmark plan  workload-shape-calibration --model nova-micro
    bedrock-benchmark pilot workload-shape-calibration --model nova-micro
    bedrock-benchmark run   workload-shape-calibration --model nova-micro

Users name experiments and models; they never pass file paths. The
filesystem layout (experiments/, catalog/, constraints/) is resolved here
and is not part of the interface.

A thin layer over the existing engine -- batch.plan / run_batch and
pilot.run_pilot_sync, exactly what scripts/run_all.py calls (kept for
backward compatibility, not the public API).
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import List, Optional

import yaml

_ROOT_MARKERS = ("experiments", "catalog/models.yaml", "constraints/slo.yaml")


def find_root(start: Optional[Path] = None) -> Path:
    """The benchmark's working tree: $BEDROCK_BENCHMARK_HOME, else the
    current directory or one of its parents, else the checkout this
    package was installed from (pip install -e .)."""
    env = os.environ.get("BEDROCK_BENCHMARK_HOME")
    candidates = [Path(env)] if env else []
    here = (start or Path.cwd()).resolve()
    candidates += [here, *here.parents, Path(__file__).resolve().parents[2]]
    for c in candidates:
        if all((c / m).exists() for m in _ROOT_MARKERS):
            return c
    raise SystemExit("bedrock-benchmark: can't find the benchmark checkout (experiments/, catalog/, constraints/) -- "
                     "run from inside it or set BEDROCK_BENCHMARK_HOME")


def experiment_names(root: Path) -> List[str]:
    return sorted(p.stem for p in (root / "experiments").glob("*.yaml"))


def experiment_paths(names: List[str], root: Path) -> List[str]:
    """Experiment names -> their files; `all` = every experiment."""
    known = experiment_names(root)
    if names == ["all"]:
        return [str(root / "experiments" / f"{n}.yaml") for n in known]
    unknown = [n for n in names if n not in known]
    if unknown:
        raise SystemExit(f"bedrock-benchmark: unknown experiment(s) {unknown} -- available: {', '.join(known)} "
                         f"(see `bedrock-benchmark list`)")
    return [str(root / "experiments" / f"{n}.yaml") for n in names]


def _models(args):
    from .constraints import current_account_id
    from .models import load_models
    models = load_models(names=args.models, account=args.account or current_account_id())
    if not models:
        raise SystemExit("bedrock-benchmark: no enabled models (catalog/models.yaml)")
    return models


def cmd_list(args, root: Path) -> int:
    """Models and experiments -- reads the catalog only, no AWS calls."""
    raw = yaml.safe_load((root / "catalog/models.yaml").read_text()) or {}
    print("Models")
    for m in raw.get("models") or []:
        state = "" if m.get("enabled", True) else "  (disabled)"
        print(f"  {m['name']:<16} {m.get('model_id', '')}{state}")
    print("\nExperiments")
    for name in experiment_names(root):
        spec = yaml.safe_load((root / "experiments" / f"{name}.yaml").read_text()) or {}
        workloads = spec.get("workloads") or []
        print(f"  {name:<28} {spec.get('purpose', ''):<22} workloads: {', '.join(workloads)}")
    print("\nUsage: bedrock-benchmark plan|pilot|run <experiment> --model <model>")
    return 0


def cmd_plan(args, root: Path, paths: List[str]) -> int:
    """Validate config + quota and estimate runtime. No requests sent."""
    from .batch import format_plan, plan
    from .experiments.schema import NoMatchingWorkloads, load_experiment
    models = _models(args)
    for model in models:
        for path in paths:
            try:
                spec = load_experiment(path, model, only_slo_profiles=args.slo_profiles)
            except NoMatchingWorkloads as skip:
                print(f"\n{model.name} / {Path(path).stem}: skipped -- {skip}")
                continue
            from .run_file import estimated_duration_s
            print(f"\nModel:       {model.name} ({model.model_id})")
            print(f"Experiment:  {spec.name}  [{spec.purpose}]")
            print(f"AWS account: {model.account}   Region: {model.region}")
            print(f"Quota:       RPM {model.quota_rpm:,.0f}   TPM {model.quota_tpm:,.0f}"
                  if model.quota_rpm and model.quota_tpm else "Quota:       unknown")
            print("Workloads:")
            for w in spec.workloads:
                ceiling = spec.provider_ceilings.get(w.name)
                ceil = f"ceiling {ceiling.rps:.4g} rps ({ceiling.binding})" if ceiling and ceiling.rps else ""
                print(f"  {w.name:<28} {w.input_tokens:>6} in / {w.output_tokens:<5} out  {w.slo_profile:<7} {ceil}")
            if spec.mix is not None:
                print(f"  mix {spec.mix.name}: {spec.mix.weights}")
            print(f"Estimated duration: ~{estimated_duration_s(spec) / 60:.0f} min"
                  + (" (upper bound: stops after consecutive FAILs)" if spec.sweep.stop_after_fails else ""))
    print()
    print(format_plan(plan(paths, models, only_slo_profiles=args.slo_profiles)))
    print("\nNo requests sent.")
    return 0


def cmd_pilot(args, root: Path, paths: List[str], target_factory=None) -> int:
    """A few sequential requests per model x workload: access, workload
    shape, SLO reachability, throttling. Then exit -- no benchmark."""
    from .pilot import format_check, run_pilot_sync
    models = _models(args)
    print(f"PILOT: {args.pilot_requests} sequential requests per model x workload")
    report = run_pilot_sync(paths, models, requests_per_workload=args.pilot_requests,
                            only_slo_profiles=args.slo_profiles, target_factory=target_factory,
                            on_check=lambda c: print(format_check(c), flush=True))
    out = Path(args.results_dir or f"results/pilot-{time.strftime('%Y%m%d-%H%M%S')}")
    out.mkdir(parents=True, exist_ok=True)
    (out / "pilot.yaml").write_text(yaml.safe_dump(report.to_dict(), sort_keys=False, width=120))
    print(f"\npilot: {report.to_dict()['summary']} -> {out / 'pilot.yaml'}")
    return report.exit_code


def cmd_run(args, root: Path, paths: List[str], target_factory=None) -> int:
    """The benchmark: capacity-profile.yaml + raw JSONL per model x experiment."""
    if args.dry_run:
        return cmd_plan(args, root, paths)
    if args.pilot:
        return cmd_pilot(args, root, paths, target_factory=target_factory)
    from .batch import format_plan, format_summary, plan, run_batch
    models = _models(args)
    print(format_plan(plan(paths, models, only_slo_profiles=args.slo_profiles)))
    results_dir = Path(args.results_dir or f"results/run-all-{time.strftime('%Y%m%d-%H%M%S')}")
    batch = run_batch(paths, models, results_dir=results_dir, fail_fast=args.fail_fast,
                      only_slo_profiles=args.slo_profiles, target_factory=target_factory)
    print(f"\n{'=' * 78}\nSUMMARY\n{'=' * 78}")
    print(format_summary(batch))
    print(f"\nresults: {results_dir.resolve()}")
    return batch.exit_code


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bedrock-benchmark", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True, metavar="{list,plan,pilot,run}")
    sub.add_parser("list", help="models and experiments (no AWS calls)")

    def with_target(sp):
        sp.add_argument("experiments", nargs="+", metavar="EXPERIMENT",
                        help="experiment name(s), e.g. workload-shape-calibration, or `all`")
        sp.add_argument("--model", action="append", dest="models", metavar="NAME",
                        help="model name (repeatable; default: every enabled model)")
        sp.add_argument("--slo-profile", action="append", dest="slo_profiles", metavar="NAME",
                        help="only workloads bound to this SLO profile, e.g. gold (repeatable)")
        sp.add_argument("--account", help="AWS account whose quotas apply (default: the live account)")
        return sp

    with_target(sub.add_parser("plan", help="validate config + quota, estimate runtime; no requests sent"))
    pilot = with_target(sub.add_parser("pilot", help="smoke test: a few requests per model x workload"))
    pilot.add_argument("--pilot-requests", type=int, default=3, metavar="N")
    pilot.add_argument("--results-dir", help="default: results/pilot-<timestamp>")
    run = with_target(sub.add_parser("run", help="run the benchmark -> capacity-profile.yaml + raw JSONL"))
    run.add_argument("--dry-run", action="store_true", help="same as `plan`")
    run.add_argument("--pilot", action="store_true", help="same as `pilot`")
    run.add_argument("--pilot-requests", type=int, default=3, metavar="N")
    run.add_argument("--results-dir", help="default: results/run-all-<timestamp>")
    run.add_argument("--fail-fast", action="store_true", help="stop at the first failed run")
    return p


def main(argv: Optional[List[str]] = None, *, target_factory=None) -> int:
    args = _parser().parse_args(argv)
    if getattr(args, "results_dir", None):
        args.results_dir = str(Path(args.results_dir).resolve())  # relative to where the user ran it
    root = find_root()
    os.chdir(root)  # the engine's catalog/constraints/results paths are relative to the checkout
    if args.command == "list":
        return cmd_list(args, root)
    paths = experiment_paths(args.experiments, root)
    if args.command == "plan":
        return cmd_plan(args, root, paths)
    if args.command == "pilot":
        return cmd_pilot(args, root, paths, target_factory=target_factory)
    return cmd_run(args, root, paths, target_factory=target_factory)


if __name__ == "__main__":
    sys.exit(main())
