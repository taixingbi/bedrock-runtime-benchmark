"""ExperimentSpec -- the YAML-loadable description of a sweep: one or
more workload profiles and a sweep dimension (concurrency OR rate --
see runners/ for why these are kept separate, not combined into one
experiment).

Experiment files are MODEL-AGNOSTIC: no `target:`, no quota, no model
name. `load_experiment(path, model)` binds one file to one model from
the models file (see models.py), filling in the target and quota --
so the same experiment runs unchanged against every model.

Rate sweeps are written as `quota_fractions` of the bound model's
PROVIDER CEILING for each sweep subject -- min(RPM, TPM / tokens per
request), see ceiling.py -- because quotas differ by an order of
magnitude across models and RPM vs TPM binds differently per workload
shape. Fixed rps values would be far over one model's ceiling and
nowhere near another's.

Workloads come from the catalog (catalog/workloads.yaml) and SLOs from
constraints/slo.yaml -- never from the experiment, which only LISTS
workload names. Each catalog workload binds its slo_profile explicitly
(there is no default), so the same workload is judged identically in
every experiment (see constraints.py, workload.py).
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Collection, Dict, List, Optional

import yaml

from ..ceiling import ProviderCeiling, provider_ceiling
from ..client import TransportConfig
from ..constraints import DEFAULT_SLO_FILE, SloConfig, load_slo
from ..models import ModelConfig
from ..workload import DEFAULT_WORKLOADS_FILE, WorkloadProfile, load_workloads


@dataclass
class TargetConfig:
    model_id: str
    region: str = "us-east-1"


@dataclass
class QuotaSnapshot:
    """Documented context for the capacity-profile.yaml artifact and
    for a human reading the experiment -- NOT enforced by this repo.
    Real RPM/TPM enforcement is Bedrock's own; this repo only ever
    measures what actually happens, it never simulates or caps against
    a quota number itself. Named "snapshot" (not "config") because it's
    a point-in-time fact about the account/region, not something this
    tool configures or controls."""
    rpm: Optional[float] = None
    tpm: Optional[float] = None


@dataclass
class SweepConfig:
    type: str  # "concurrency" | "rate"
    # Absolute values: concurrency levels, or rps. Resolved from
    # quota_fractions at bind time for a quota-relative rate sweep.
    values: List[float] = field(default_factory=list)
    # Rate sweeps only: fractions of each subject's provider ceiling
    # (value_rps = fraction * ceiling_rps; see ceiling.py).
    quota_fractions: Optional[List[float]] = None

    @property
    def point_count(self) -> int:
        return len(self.quota_fractions) if self.quota_fractions is not None else len(self.values)


@dataclass
class ConfirmationConfig:
    """Two-phase sweep: the discovery pass (every value, `repetitions`
    each) finds the transition region; this phase re-runs the candidate
    safe point and `neighbors` points either side of it `repetitions`
    more times, pooled with discovery. One 90s window is a capacity
    snapshot; the final capacity-profile should rest on repeated
    measurements at the boundary, not on every point equally."""
    repetitions: int = 3
    neighbors: int = 1


@dataclass
class MixConfig:
    """Mixed-workload experiment: instead of sweeping each workload in
    isolation, sweep ONE offered load (rate) or concurrency where each
    request independently draws its class by weight. The only valid
    source of a cross-class envelope -- isolated per-class maxima can't
    be combined into one (see report.py)."""
    name: str
    weights: Dict[str, float] = field(default_factory=dict)


@dataclass
class ExperimentSpec:
    name: str  # the experiment's name -- never contains a model name
    target: TargetConfig
    workloads: List[WorkloadProfile]
    sweep: SweepConfig
    description: str = ""
    quota_snapshot: QuotaSnapshot = field(default_factory=QuotaSnapshot)
    slo: SloConfig = field(default_factory=SloConfig)
    duration_s: float = 60.0
    # Load runs for warmup_s before the measurement window opens
    # (connection pool / TLS / cold paths) -- none of it is counted.
    warmup_s: float = 0.0
    # Each sweep point runs this many times back to back; the SLO gate
    # reads the pooled window, and per-repetition metrics are kept so
    # run-to-run spread is visible.
    repetitions: int = 1
    stream: bool = True
    # Back-off from the MEASURED safe rate for production.
    provider_headroom: float = 0.20
    # Back-off from the provider CEILING (quota) for production: a sweep
    # that passed above quota may have ridden Bedrock's short-window
    # burst allowance, which isn't sustainable, so production rate is
    # min(statistically_confirmed x (1 - provider_headroom), ceiling x (1 - quota_headroom)).
    quota_headroom: float = 0.10
    confirmation: Optional[ConfirmationConfig] = None
    seed: Optional[int] = None
    transport: TransportConfig = field(default_factory=TransportConfig)
    mix: Optional[MixConfig] = None
    # Post-run check: Bedrock-REPORTED input_tokens p50 vs requested.
    # Outside this, the class's workload_validation is valid: false
    # (the 4-chars/token padding estimate missed for this model).
    workload_validation_tolerance_pct: float = 10.0
    # Post-run check: Bedrock-reported output_tokens p50 vs the target
    # (max_tokens). Looser than input -- models legitimately stop a bit
    # early -- but a "512 out" class that really emits 110 is flagged.
    output_validation_tolerance_pct: float = 25.0
    # Padding calibration (calibration.py): strategy from the model entry,
    # tolerance for counted vs requested input tokens.
    token_counting: str = "auto"
    calibration_tolerance_pct: float = 2.0
    output_burndown: float = 1.0  # from constraints/quota.yaml, for the artifact
    quota_account: Optional[str] = None  # the AWS account the quota was taken for
    # Every profile from constraints/slo.yaml; each workload names one via
    # WorkloadProfile.slo_profile. For a loaded spec, `slo` above is the
    # STRICTEST success/throttle gate among the profiles its workloads
    # use (no latency) -- what a mixed blend and the sample-size check
    # are held to.
    slo_profiles: Dict[str, SloConfig] = field(default_factory=dict)
    # The models-file entry this spec is bound to (None only for specs
    # built directly in code, e.g. tests).
    model_name: Optional[str] = None
    # Per sweep subject (workload name, or the mix name): the quota's
    # theoretical request ceiling for that subject's token shape.
    provider_ceilings: Dict[str, ProviderCeiling] = field(default_factory=dict)

    def slo_for(self, workload_name: str) -> SloConfig:
        """The workload's profile (TTFT/TPOT/success/throttle), with the
        workload's own E2E latency cap applied on top."""
        workload = next((w for w in self.workloads if w.name == workload_name), None)
        slo = self.slo
        if workload is not None and workload.slo_profile is not None:
            slo = self.slo_profiles[workload.slo_profile]
        if workload is not None and workload.latency_p95_ms is not None:
            slo = replace(slo, latency_p95_ms=workload.latency_p95_ms)
        return slo

    @property
    def subject_names(self) -> List[str]:
        return [self.mix.name] if self.mix is not None else [w.name for w in self.workloads]

    def sweep_values(self, subject_name: str) -> List[float]:
        """Absolute sweep values for one subject -- quota-relative rate
        sweeps resolve against that subject's own provider ceiling."""
        if self.sweep.quota_fractions is None:
            return list(self.sweep.values)
        ceiling = self.provider_ceilings[subject_name].rps
        return [round(f * ceiling, 4) for f in self.sweep.quota_fractions]


class NoMatchingWorkloads(Exception):
    """Raised by load_experiment when an --slo-profile filter leaves the
    experiment nothing to run -- a skip, not an error."""


_MODEL_KEYS = ("target", "quota_snapshot", "quota")
_SLO_KEYS = ("slo", "slo_profiles")


def load_experiment(
    path: str, model: ModelConfig, *, slo_file: str = DEFAULT_SLO_FILE, workloads_file: str = DEFAULT_WORKLOADS_FILE,
    only_slo_profiles: Optional[Collection[str]] = None,
) -> ExperimentSpec:
    """only_slo_profiles (e.g. {"gold"}) keeps just the workloads bound to
    those profiles. An isolated sweep keeps its matching workloads; a mix
    runs only if EVERY class matches (dropping classes would silently
    make it a different mix); with nothing left, NoMatchingWorkloads is
    raised so the caller can skip the experiment."""
    raw = yaml.safe_load(Path(path).read_text())

    present = [k for k in _MODEL_KEYS if k in raw]
    if present:
        raise ValueError(
            f"{path}: experiments are model-agnostic -- remove {present}; models live in "
            f"catalog/models.yaml and quotas in constraints/quota.yaml"
        )
    present = [k for k in _SLO_KEYS if k in raw]
    if present:
        raise ValueError(
            f"{path}: remove {present} -- SLOs are defined once in {slo_file}; "
            f"give a workload `slo_profile: <name>` to use a non-default one"
        )
    slos = load_slo(slo_file)
    sweep = raw["sweep"]
    transport = raw.get("transport") or {}
    workloads = _resolve_workloads(raw.get("workloads"), path, workloads_file)
    if only_slo_profiles is not None:
        workloads = _filter_by_slo_profile(workloads, raw, set(only_slo_profiles), slos.profiles, slo_file)

    spec = ExperimentSpec(
        name=raw["name"],
        description=raw.get("description", ""),
        target=TargetConfig(model_id=model.model_id, region=model.region),
        quota_snapshot=QuotaSnapshot(rpm=model.quota_rpm, tpm=model.quota_tpm),
        slo=SloConfig(),  # replaced by the strictest used gate below, after validation
        workloads=workloads,
        duration_s=raw.get("duration_s", 60.0),
        warmup_s=raw.get("warmup_s", 0.0),
        repetitions=raw.get("repetitions", 1),
        stream=raw.get("stream", True),
        sweep=SweepConfig(**sweep),
        provider_headroom=raw.get("provider_headroom", 0.20),
        quota_headroom=raw.get("quota_headroom", 0.10),
        confirmation=ConfirmationConfig(**raw["confirmation"]) if raw.get("confirmation") else None,
        seed=raw.get("seed"),
        transport=TransportConfig(**transport),
        mix=MixConfig(**raw["mix"]) if raw.get("mix") else None,
        workload_validation_tolerance_pct=raw.get("workload_validation_tolerance_pct", 10.0),
        output_validation_tolerance_pct=raw.get("output_validation_tolerance_pct", 25.0),
        slo_profiles=dict(slos.profiles),
        token_counting=model.token_counting,
        output_burndown=model.output_burndown,
        quota_account=model.account,
        calibration_tolerance_pct=raw.get("calibration_tolerance_pct", 2.0),
        model_name=model.name,
    )
    _validate(spec)
    spec.slo = _strictest_gate([spec.slo_profiles[w.slo_profile] for w in spec.workloads])
    spec.provider_ceilings = _ceilings(spec, model)
    _validate_sweep(spec, model, path)
    return spec


def _filter_by_slo_profile(workloads, raw, only, defined, slo_file: str) -> List[WorkloadProfile]:
    unknown = sorted(only - set(defined))
    if unknown:
        raise ValueError(f"--slo-profile {unknown} not defined in {slo_file} (has: {sorted(defined)})")
    if raw.get("mix"):
        classes = set((raw["mix"].get("weights") or {}))
        others = sorted(f"{w.name} ({w.slo_profile})" for w in workloads if w.name in classes and w.slo_profile not in only)
        if others:
            raise NoMatchingWorkloads(
                f"mix {raw['mix'].get('name')!r} also includes {', '.join(others)} -- a partial mix is a different mix"
            )
        return workloads
    kept = [w for w in workloads if w.slo_profile in only]
    if not kept:
        raise NoMatchingWorkloads(f"no workloads bound to {sorted(only)}")
    return kept


def _resolve_workloads(names, path: str, workloads_file: str) -> List[WorkloadProfile]:
    if not isinstance(names, list) or not names or not all(isinstance(n, str) for n in names):
        raise ValueError(
            f"{path}: `workloads:` must be a list of workload names from {workloads_file} "
            f"(e.g. [short_chat]) -- shapes and SLO bindings are defined there, not in experiments"
        )
    catalog = load_workloads(workloads_file)
    unknown = [n for n in names if n not in catalog]
    if unknown:
        raise ValueError(f"{path}: unknown workloads {unknown}; {workloads_file} defines {sorted(catalog)}")
    if len(set(names)) != len(names):
        raise ValueError(f"{path}: workloads listed twice: {names}")
    return [catalog[n] for n in names]


def _strictest_gate(profiles: List[SloConfig]) -> SloConfig:
    """Success/throttle gates only -- latency always comes from each
    workload's own profile."""
    confidences = [p.confidence for p in profiles if p.confidence is not None]
    return SloConfig(
        success_rate_min=max(p.success_rate_min for p in profiles),
        throttle_rate_max=min(p.throttle_rate_max for p in profiles),
        confidence=max(confidences) if confidences else None,
    )


def _ceilings(spec: ExperimentSpec, model: ModelConfig) -> Dict[str, ProviderCeiling]:
    kwargs = dict(rpm=model.quota_rpm, tpm=model.quota_tpm, output_burndown=model.output_burndown)
    by_name = {w.name: w for w in spec.workloads}
    if spec.mix is not None:
        classes = [(by_name[n], w) for n, w in spec.mix.weights.items() if n in by_name]
        return {spec.mix.name: provider_ceiling(classes, **kwargs)} if classes else {}
    return {w.name: provider_ceiling([(w, 1.0)], **kwargs) for w in spec.workloads}


def _validate_sweep(spec: ExperimentSpec, model: ModelConfig, path: str) -> None:
    sweep = spec.sweep
    if sweep.quota_fractions is None:
        if not sweep.values:
            raise ValueError(f"{path}: sweep needs `values` or (rate only) `quota_fractions`")
    else:
        if sweep.values:
            raise ValueError(f"{path}: sweep takes `values` OR `quota_fractions`, not both")
        if sweep.type != "rate":
            raise ValueError(f"{path}: quota_fractions only applies to rate sweeps")
        if not sweep.quota_fractions or any(f <= 0 for f in sweep.quota_fractions):
            raise ValueError(f"{path}: quota_fractions must be non-empty and all > 0")
        missing = [n for n in spec.subject_names if spec.provider_ceilings.get(n) is None
                   or spec.provider_ceilings[n].rps is None]
        if missing:
            raise ValueError(
                f"{path}: quota-relative rate sweep needs quota.rpm or quota.tpm for model {model.name!r} "
                f"in the models file (scripts/fetch_quota.py --all)"
            )
    if sweep.type == "concurrency" and sweep.values and max(sweep.values) > spec.transport.max_connections:
        raise ValueError(
            f"{path}: concurrency {max(sweep.values):g} exceeds transport.max_connections "
            f"({spec.transport.max_connections}) -- the connection pool would cap in-flight calls, "
            f"measuring the client instead of Bedrock"
        )


def _validate(spec: ExperimentSpec) -> None:
    for name in ("provider_headroom", "quota_headroom"):
        if not 0 <= getattr(spec, name) < 1:
            raise ValueError(f"{name} must be in [0, 1)")
    if spec.confirmation is not None and (spec.confirmation.repetitions < 1 or spec.confirmation.neighbors < 0):
        raise ValueError("confirmation.repetitions must be >= 1 and confirmation.neighbors >= 0")
    if spec.repetitions < 1:
        raise ValueError(f"repetitions must be >= 1, got {spec.repetitions}")
    if spec.warmup_s < 0 or spec.duration_s <= 0:
        raise ValueError("warmup_s must be >= 0 and duration_s > 0")
    if spec.slo.confidence is not None and not 0 < spec.slo.confidence < 1:
        raise ValueError(f"slo.confidence must be in (0, 1), got {spec.slo.confidence}")
    from ..calibration import STRATEGIES
    if spec.token_counting not in STRATEGIES:
        raise ValueError(f"token_counting must be one of {STRATEGIES}, got {spec.token_counting!r}")
    unknown_profiles = sorted({w.slo_profile for w in spec.workloads if w.slo_profile} - set(spec.slo_profiles))
    if unknown_profiles:
        raise ValueError(f"workloads bind SLO profiles not defined in the SLO file: {unknown_profiles}")
    for name, profile in spec.slo_profiles.items():
        if profile.confidence is not None and not 0 < profile.confidence < 1:
            raise ValueError(f"slo_profiles.{name}.confidence must be in (0, 1)")
    if spec.mix is not None:
        names = {w.name for w in spec.workloads}
        unknown = set(spec.mix.weights) - names
        if unknown:
            raise ValueError(f"mix {spec.mix.name!r} references undefined workloads: {sorted(unknown)}")
        if not spec.mix.weights or any(w <= 0 for w in spec.mix.weights.values()):
            raise ValueError(f"mix {spec.mix.name!r} needs at least one workload, all weights > 0")
