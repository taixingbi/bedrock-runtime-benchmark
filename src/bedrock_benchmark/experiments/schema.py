"""ExperimentSpec -- the YAML-loadable description of a sweep:
one model target, one or more workload profiles, and a sweep dimension
(concurrency OR rate -- see runners/ for why these are kept separate,
not combined into one experiment). One YAML file == one
`python scripts/run.py <file>.yaml` invocation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

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


@dataclass
class SweepConfig:
    type: str  # "concurrency" | "rate"
    values: List[float] = field(default_factory=list)


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
    stream: bool = True
    provider_headroom: float = 0.20
    seed: Optional[int] = None
    transport: TransportConfig = field(default_factory=TransportConfig)


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

    return ExperimentSpec(
        name=raw["name"],
        description=raw.get("description", ""),
        target=TargetConfig(**target),
        quota_snapshot=QuotaSnapshot(**quota_snapshot),
        slo=SloConfig(**slo),
        workloads=workloads,
        duration_s=raw.get("duration_s", 60.0),
        stream=raw.get("stream", True),
        sweep=SweepConfig(**sweep),
        provider_headroom=raw.get("provider_headroom", 0.20),
        seed=raw.get("seed"),
        transport=TransportConfig(**transport),
    )
