"""ExperimentSpec -- the YAML-loadable description of a sweep: one or
more workload profiles and a sweep dimension (concurrency OR rate --
see runners/ for why these are kept separate, not combined into one
experiment).

Experiment files are MODEL-AGNOSTIC: no `target:`, no quota, no model
name. `load_experiment(path, model)` binds one file to one model from
the models file (see models.py), filling in the target and quota --
so the same experiment runs unchanged against every model.

Rate sweeps are written as `quota_fractions` of the bound model's own
RPM quota (1.0 = exactly the quota), because quotas differ by an order
of magnitude across models -- fixed rps values would be far over one
model's ceiling and nowhere near another's.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from ..client import TransportConfig
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
class SloConfig:
    ttft_p95_ms: Optional[float] = None
    latency_p95_ms: Optional[float] = None
    # Result-quality gates -- distinct from the two latencies above
    # (response SPEED), these are about whether responses came back at
    # all and cleanly. Folded into SloConfig (schema_version 2) rather
    # than kept as separate top-level ExperimentSpec fields, since
    # they're conceptually part of "what counts as meeting the SLO"
    # the same way the two latency thresholds are.
    success_rate_min: float = 0.99
    throttle_rate_max: float = 0.001
    # When set (e.g. 0.95), success_rate_min/throttle_rate_max are
    # gated on one-sided Wilson confidence bounds instead of point
    # estimates -- a point has to have enough requests to DEMONSTRATE
    # it meets a 0.1% throttle SLO (~2,700 at 95%), not just happen to
    # observe zero throttles in a few hundred. None keeps point-
    # estimate gating (bounds are still computed and reported at 95%).
    confidence: Optional[float] = None


@dataclass
class SweepConfig:
    type: str  # "concurrency" | "rate"
    # Absolute values: concurrency levels, or rps. Resolved from
    # quota_fractions at bind time for a quota-relative rate sweep.
    values: List[float] = field(default_factory=list)
    # Rate sweeps only: fractions of the model's quota RPM
    # (value_rps = fraction * rpm / 60).
    quota_fractions: Optional[List[float]] = None


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
    # The models-file entry this spec is bound to (None only for specs
    # built directly in code, e.g. tests).
    model_name: Optional[str] = None


_MODEL_KEYS = ("target", "quota_snapshot", "quota")


def load_experiment(path: str, model: ModelConfig) -> ExperimentSpec:
    raw = yaml.safe_load(Path(path).read_text())

    present = [k for k in _MODEL_KEYS if k in raw]
    if present:
        raise ValueError(
            f"{path}: experiments are model-agnostic -- remove {present}; models and their quotas "
            f"live in the models file (scripts/models.yaml)"
        )
    slo = raw.get("slo") or {}
    sweep = raw["sweep"]
    transport = raw.get("transport") or {}
    workloads = [WorkloadProfile(**w) for w in raw["workloads"]]

    spec = ExperimentSpec(
        name=raw["name"],
        description=raw.get("description", ""),
        target=TargetConfig(model_id=model.model_id, region=model.region),
        quota_snapshot=QuotaSnapshot(rpm=model.quota_rpm, tpm=model.quota_tpm),
        slo=SloConfig(**slo),
        workloads=workloads,
        duration_s=raw.get("duration_s", 60.0),
        warmup_s=raw.get("warmup_s", 0.0),
        repetitions=raw.get("repetitions", 1),
        stream=raw.get("stream", True),
        sweep=_resolve_sweep(SweepConfig(**sweep), model, path),
        provider_headroom=raw.get("provider_headroom", 0.20),
        seed=raw.get("seed"),
        transport=TransportConfig(**transport),
        mix=MixConfig(**raw["mix"]) if raw.get("mix") else None,
        workload_validation_tolerance_pct=raw.get("workload_validation_tolerance_pct", 10.0),
        model_name=model.name,
    )
    _validate(spec)
    return spec


def _resolve_sweep(sweep: SweepConfig, model: ModelConfig, path: str) -> SweepConfig:
    if sweep.quota_fractions is None:
        if not sweep.values:
            raise ValueError(f"{path}: sweep needs `values` or (rate only) `quota_fractions`")
        return sweep
    if sweep.values:
        raise ValueError(f"{path}: sweep takes `values` OR `quota_fractions`, not both")
    if sweep.type != "rate":
        raise ValueError(f"{path}: quota_fractions only applies to rate sweeps")
    if not sweep.quota_fractions or any(f <= 0 for f in sweep.quota_fractions):
        raise ValueError(f"{path}: quota_fractions must be non-empty and all > 0")
    if not model.quota_rpm:
        raise ValueError(
            f"{path}: quota-relative rate sweep needs quota.rpm for model {model.name!r} in the models file "
            f"(scripts/fetch_quota.py --all)"
        )
    rps = model.quota_rpm / 60.0
    return SweepConfig(
        type=sweep.type, values=[round(f * rps, 4) for f in sweep.quota_fractions],
        quota_fractions=list(sweep.quota_fractions),
    )


def _validate(spec: ExperimentSpec) -> None:
    if spec.repetitions < 1:
        raise ValueError(f"repetitions must be >= 1, got {spec.repetitions}")
    if spec.warmup_s < 0 or spec.duration_s <= 0:
        raise ValueError("warmup_s must be >= 0 and duration_s > 0")
    if spec.slo.confidence is not None and not 0 < spec.slo.confidence < 1:
        raise ValueError(f"slo.confidence must be in (0, 1), got {spec.slo.confidence}")
    if spec.mix is not None:
        names = {w.name for w in spec.workloads}
        unknown = set(spec.mix.weights) - names
        if unknown:
            raise ValueError(f"mix {spec.mix.name!r} references undefined workloads: {sorted(unknown)}")
        if not spec.mix.weights or any(w <= 0 for w in spec.mix.weights.values()):
            raise ValueError(f"mix {spec.mix.name!r} needs at least one workload, all weights > 0")
