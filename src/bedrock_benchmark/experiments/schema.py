"""ExperimentSpec -- the YAML-loadable description of a sweep:
one model target, one or more workload profiles, and a sweep dimension
(concurrency OR rate -- see runners/ for why these are kept separate,
not combined into one experiment). One YAML file == one
`python scripts/run.py <file>.yaml` invocation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from ..client import TransportConfig
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
    values: List[float] = field(default_factory=list)


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
    name: str
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


def load_experiment(path: str) -> ExperimentSpec:
    raw = yaml.safe_load(Path(path).read_text())

    target = raw["target"]
    # Accepts both `quota_snapshot:` (current) and `quota:` (the
    # pre-schema_version-2 key) so an old experiment YAML lying around
    # doesn't silently lose its documented quota context.
    quota_snapshot = raw.get("quota_snapshot") or raw.get("quota") or {}
    slo = raw.get("slo") or {}
    sweep = raw["sweep"]
    transport = raw.get("transport") or {}
    workloads = [WorkloadProfile(**w) for w in raw["workloads"]]

    spec = ExperimentSpec(
        name=raw["name"],
        description=raw.get("description", ""),
        target=TargetConfig(**target),
        quota_snapshot=QuotaSnapshot(**quota_snapshot),
        slo=SloConfig(**slo),
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
    )
    _validate(spec)
    return spec


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
