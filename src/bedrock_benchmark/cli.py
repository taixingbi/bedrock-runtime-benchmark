"""bedrock-benchmark -- the public command line. The paved road:

    bedrock-benchmark doctor   --model nova-micro                         # ready? (identity, access, quota, config)
    bedrock-benchmark plan     workload-shape-calibration --model nova-micro   # what will run, how long; no requests
    bedrock-benchmark pilot    workload-shape-calibration --model nova-micro   # a few requests per workload
    bedrock-benchmark run      workload-shape-calibration --model nova-micro --ticket CAP-123
    bedrock-benchmark summary  results/run-all-<ts>                        # can I trust it / what did we learn
    bedrock-benchmark validate results/                                    # temporal validation across runs
    bedrock-benchmark publish  results/run-all-<ts> --destination s3://bucket/prefix
    bedrock-benchmark validate-profile <artifact.yaml>                     # machine contract (JSON Schema)
    bedrock-benchmark list

Users name experiments and models; they never pass file paths into the
checkout. Expensive commands (pilot, run) never default to every model:
pass --model, or --all-models explicitly.

A thin layer over the engine -- batch.plan / run_batch, pilot,
drift.build_temporal_profile, contract, publish. scripts/*.py are kept for
backward compatibility and are not the public API.
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
    if not args.models and not args.all_models:
        raise SystemExit(f"bedrock-benchmark {args.command}: pass --model NAME (repeatable), or --all-models to "
                         f"target every enabled model explicitly")
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
        if not workloads and spec.get("mix"):
            from .experiments.schema import load_mixes
            mix = spec["mix"]
            weights = mix.get("weights")
            if weights is None:
                weights = load_mixes(str(root / "catalog/mixes.yaml"))[mix["name"]]["weights"]
            workloads = list(weights)
        print(f"  {name:<28} {spec.get('purpose', ''):<22} workloads: {', '.join(workloads)}")
    print("\nStart with: bedrock-benchmark doctor --model <model>, then plan / pilot / run <experiment> --model <model>")
    return 0


def cmd_plan(args, root: Path, paths: List[str]) -> int:
    """Validate config + quota and estimate runtime. No requests sent."""
    from .batch import format_plan, plan
    from .experiments.schema import NoMatchingWorkloads, load_experiment
    models = _models(args)
    for model in models:
        for path in paths:
            try:
                spec = load_experiment(path, model, only_slo_profiles=args.slo_profiles, mix=args.mix)
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
                print(f"  mix {spec.mix.name}: {spec.mix.weights} "
                      f"({spec.mix.assignment}, source={spec.mix.source})")
            print(f"Estimated duration: ~{estimated_duration_s(spec) / 60:.0f} min"
                  + (" (upper bound: stops after consecutive FAILs)" if spec.sweep.stop_after_fails else ""))
    print()
    print(format_plan(plan(paths, models, only_slo_profiles=args.slo_profiles, mix=args.mix)))
    print("\nNo requests sent.")
    return 0


def cmd_pilot(args, root: Path, paths: List[str], target_factory=None) -> int:
    """A few sequential requests per model x workload: access, workload
    shape, SLO reachability, throttling. Then exit -- no benchmark."""
    from .pilot import format_check, run_pilot_sync
    models = _models(args)
    print(f"PILOT: {args.pilot_requests} sequential requests per model x workload")
    report = run_pilot_sync(paths, models, requests_per_workload=args.pilot_requests,
                            only_slo_profiles=args.slo_profiles, mix=args.mix, target_factory=target_factory,
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
    print(format_plan(plan(paths, models, only_slo_profiles=args.slo_profiles, mix=args.mix)))
    results_dir = Path(args.results_dir or f"results/run-all-{time.strftime('%Y%m%d-%H%M%S')}")
    batch = run_batch(paths, models, results_dir=results_dir, fail_fast=args.fail_fast,
                      only_slo_profiles=args.slo_profiles, mix=args.mix, target_factory=target_factory,
                      run_metadata=run_metadata(args))
    print(f"\n{'=' * 78}\nSUMMARY\n{'=' * 78}")
    print(format_summary(batch))
    from .summary import summarize_profile
    for r in batch.results:
        if r.profile_path:
            print("\n" + summarize_profile(yaml.safe_load(Path(r.profile_path).read_text())))
    print(f"\nresults: {results_dir.resolve()}")
    print(f"next:    bedrock-benchmark publish {results_dir} --destination <team location>")
    return batch.exit_code


def run_metadata(args) -> dict:
    """The profile's `run:` block -- who ran it and why."""
    import getpass
    meta = {"owner": args.owner or getpass.getuser(), "purpose": args.run_purpose,
            "ticket": args.ticket, "environment": args.environment}
    return {k: v for k, v in meta.items() if v}


def cmd_doctor(args, root: Path, aws_clients=None) -> int:
    from .doctor import FAIL, format_doctor, run_doctor
    paths = experiment_paths(args.experiments or ["all"], root)
    checks = run_doctor(args.model, paths, root, account=args.account, **(aws_clients or {}))
    print(format_doctor(args.model, checks))
    return 1 if any(c.status == FAIL for c in checks) else 0


def cmd_validate(args) -> int:
    """Temporal validation: repeated runs -> temporal-capacity-profile.yaml."""
    from .drift import build_temporal_profile, format_temporal
    missing = [p for p in args.paths if not Path(p).exists()]
    if missing:
        raise SystemExit(f"bedrock-benchmark validate: not found: {missing}")
    profile = build_temporal_profile(args.paths, stability_threshold_pct=args.threshold,
                                     min_runs=args.min_runs, min_days=args.min_days)
    if not profile["inputs"]["profiles"]:
        raise SystemExit(f"bedrock-benchmark validate: no capacity profiles under {args.paths}")
    first = Path(args.paths[0])
    out = Path(args.output) if args.output else (first if first.is_dir() else first.parent) / "temporal-capacity-profile.yaml"
    out.write_text(yaml.safe_dump(profile, sort_keys=False, width=120))
    print(format_temporal(profile))
    print(f"\n-> {out}")
    return 0


def cmd_validate_profile(args) -> int:
    from .contract import validate_artifact
    bad = 0
    for path in args.paths:
        try:
            schema, errors = validate_artifact(path)
        except (OSError, ValueError) as exc:
            print(f"FAIL {path}: {exc}")
            bad += 1
            continue
        print(f"{'ok  ' if not errors else 'FAIL'} {path}  ({schema})")
        for e in errors:
            print(f"       {e}")
        bad += bool(errors)
    return 1 if bad else 0


def _profile_files(paths: List[str]) -> List[Path]:
    out = []
    for raw in paths:
        p = Path(raw)
        found = sorted(p.rglob("*-capacity-profile.yaml")) if p.is_dir() else [p]
        out += [f for f in found if not f.name.startswith("temporal-")]
    return out


def cmd_summary(args) -> int:
    from .summary import summarize_profile
    files = _profile_files(args.paths)
    if not files:
        raise SystemExit(f"bedrock-benchmark summary: no capacity profiles under {args.paths}")
    for i, f in enumerate(files):
        print(("\n" if i else "") + f"# {f}\n" + summarize_profile(yaml.safe_load(f.read_text()) or {}))
    return 0


def cmd_publish(args, s3_client=None) -> int:
    from .publish import publish
    try:
        where, manifest = publish(args.run_dir, args.destination, owner=args.owner, s3_client=s3_client)
    except ValueError as exc:
        print(f"bedrock-benchmark publish: {exc}")
        return 1
    print(f"published {len(manifest['files'])} files ({len(manifest['profiles'])} capacity profiles), "
          f"owner {manifest['owner']} -> {where}")
    print(f"manifest: {where}/manifest.yaml")
    return 0


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bedrock-benchmark", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True,
                           metavar="{doctor,list,plan,pilot,run,summary,validate,publish,validate-profile}")
    sub.add_parser("list", help="models and experiments (no AWS calls)")

    doctor = sub.add_parser("doctor", help="is this account / model / config ready? (2-3 one-token requests)")
    doctor.add_argument("experiments", nargs="*", metavar="EXPERIMENT", help="default: all")
    doctor.add_argument("--model", metavar="NAME", required=True)
    doctor.add_argument("--account", help="AWS account whose quotas apply (default: the live account)")

    def with_target(sp):
        sp.add_argument("experiments", nargs="+", metavar="EXPERIMENT",
                        help="experiment name(s), e.g. workload-shape-calibration, or `all`")
        sp.add_argument("--model", action="append", dest="models", metavar="NAME", help="model name (repeatable)")
        sp.add_argument("--all-models", action="store_true", help="every enabled model -- never the implicit default")
        sp.add_argument("--slo-profile", action="append", dest="slo_profiles", metavar="NAME",
                        help="only workloads bound to this SLO profile, e.g. gold (repeatable)")
        sp.add_argument("--mix", metavar="NAME", help="workflow mix from catalog/mixes.yaml")
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
    run.add_argument("--owner", help="who owns this run (default: $USER)")
    run.add_argument("--purpose", dest="run_purpose", metavar="TEXT", help="why it was run, e.g. \"model onboarding\"")
    run.add_argument("--ticket", help="tracking ticket, e.g. CAP-123")
    run.add_argument("--environment", default="dev", help="dev | staging | prod account (default: dev)")

    summary = sub.add_parser("summary", help="human summary of capacity profiles (files or run directories)")
    summary.add_argument("paths", nargs="+", metavar="PATH")

    validate = sub.add_parser("validate", help="temporal validation across repeated runs -> "
                                               "temporal-capacity-profile.yaml")
    validate.add_argument("paths", nargs="*", metavar="PATH", help="profiles or directories (default: results/)")
    validate.add_argument("--output", help="default: <first directory>/temporal-capacity-profile.yaml")
    validate.add_argument("--threshold", type=float, default=20.0,
                          help="max spread %% of confirmed values for a stable envelope (default 20)")
    validate.add_argument("--min-runs", type=int, default=3, help="runs needed (default 3)")
    validate.add_argument("--min-days", type=int, default=2, help="distinct days needed (default 2)")

    publish = sub.add_parser("publish", help="copy a run + manifest to the team's shared location (immutable)")
    publish.add_argument("run_dir", metavar="RUN_DIR")
    publish.add_argument("--destination", required=True, help="a directory or s3://bucket/prefix")
    publish.add_argument("--owner", help="only for runs that predate the profile's `run:` block")

    vp = sub.add_parser("validate-profile", help="check artifacts against the machine contract (JSON Schema)")
    vp.add_argument("paths", nargs="+", metavar="ARTIFACT")
    return p


def main(argv: Optional[List[str]] = None, *, target_factory=None, aws_clients=None, s3_client=None) -> int:
    """Injection points for tests: target_factory (pilot / run), aws_clients
    (doctor: sts_client, runtime_client, quota_fetch, git), s3_client (publish)."""
    args = _parser().parse_args(argv)
    for attr in ("results_dir", "output", "run_dir"):
        if getattr(args, attr, None):
            setattr(args, attr, str(Path(getattr(args, attr)).resolve()))  # relative to where the user ran it
    if getattr(args, "paths", None):
        args.paths = [str(Path(x).resolve()) for x in args.paths]
    if args.command == "validate-profile":
        return cmd_validate_profile(args)
    if args.command == "summary":
        return cmd_summary(args)
    if args.command == "publish":
        return cmd_publish(args, s3_client=s3_client)
    root = find_root()
    os.chdir(root)  # the engine's catalog/constraints/results paths are relative to the checkout
    if args.command == "list":
        return cmd_list(args, root)
    if args.command == "validate":
        args.paths = args.paths or [str(root / "results")]
        return cmd_validate(args)
    if args.command == "doctor":
        return cmd_doctor(args, root, aws_clients=aws_clients)
    paths = experiment_paths(args.experiments, root)
    if args.command == "plan":
        return cmd_plan(args, root, paths)
    if args.command == "pilot":
        return cmd_pilot(args, root, paths, target_factory=target_factory)
    return cmd_run(args, root, paths, target_factory=target_factory)


if __name__ == "__main__":
    sys.exit(main())
