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

SLOs come from constraints/slo.yaml, never from the experiment: each
workload names a profile (`slo_profile: long_generation`) or gets the
file's default, so the same workload class is judged identically in
every experiment (see constraints.py).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from ..ceiling import ProviderCeiling, provider_ceiling
from ..client import TransportConfig
from ..constraints import DEFAULT_SLO_FILE, SloConfig, load_slo
from ..models import ModelConfig
from ..workload import WorkloadProfile


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
    provider_headroom: float = 0.20
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
    # Every profile from constraints/slo.yaml (workloads opt in via
    # WorkloadProfile.slo_profile); `slo` above is its default profile.
    slo_profiles: Dict[str, SloConfig] = field(default_factory=dict)
    slo_default: str = "default"
    # The models-file entry this spec is bound to (None only for specs
    # built directly in code, e.g. tests).
    model_name: Optional[str] = None
    # Per sweep subject (workload name, or the mix name): the quota's
    # theoretical request ceiling for that subject's token shape.
    provider_ceilings: Dict[str, ProviderCeiling] = field(default_factory=dict)

    def slo_for(self, workload_name: str) -> SloConfig:
        workload = next((w for w in self.workloads if w.name == workload_name), None)
        if workload is not None and workload.slo_profile is not None:
            return self.slo_profiles[workload.slo_profile]
        return self.slo

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


_MODEL_KEYS = ("target", "quota_snapshot", "quota")
_SLO_KEYS = ("slo", "slo_profiles")


def load_experiment(path: str, model: ModelConfig, *, slo_file: str = DEFAULT_SLO_FILE) -> ExperimentSpec:
    raw = yaml.safe_load(Path(path).read_text())

    present = [k for k in _MODEL_KEYS if k in raw]
    if present:
        raise ValueError(
            f"{path}: experiments are model-agnostic -- remove {present}; models live in "
            f"scripts/models.yaml and quotas in constraints/quota.yaml"
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
    workloads = [WorkloadProfile(**w) for w in raw["workloads"]]

    spec = ExperimentSpec(
        name=raw["name"],
        description=raw.get("description", ""),
        target=TargetConfig(model_id=model.model_id, region=model.region),
        quota_snapshot=QuotaSnapshot(rpm=model.quota_rpm, tpm=model.quota_tpm),
        slo=slos.get(None),
        workloads=workloads,
        duration_s=raw.get("duration_s", 60.0),
        warmup_s=raw.get("warmup_s", 0.0),
        repetitions=raw.get("repetitions", 1),
        stream=raw.get("stream", True),
        sweep=SweepConfig(**sweep),
        provider_headroom=raw.get("provider_headroom", 0.20),
        seed=raw.get("seed"),
        transport=TransportConfig(**transport),
        mix=MixConfig(**raw["mix"]) if raw.get("mix") else None,
        workload_validation_tolerance_pct=raw.get("workload_validation_tolerance_pct", 10.0),
        output_validation_tolerance_pct=raw.get("output_validation_tolerance_pct", 25.0),
        slo_profiles=dict(slos.profiles),
        slo_default=slos.default,
        token_counting=model.token_counting,
        output_burndown=model.output_burndown,
        calibration_tolerance_pct=raw.get("calibration_tolerance_pct", 2.0),
        model_name=model.name,
    )
    _validate(spec)
    spec.provider_ceilings = _ceilings(spec, model)
    _validate_sweep(spec, model, path)
    return spec


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
        raise ValueError(f"workloads reference SLO profiles not in the SLO file: {unknown_profiles}")
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
