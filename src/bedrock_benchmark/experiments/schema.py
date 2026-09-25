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

from ..workload import WorkloadProfile


@dataclass
class TargetConfig:
    model_id: str
    region: str = "us-east-1"


@dataclass
class QuotaConfig:
    """Documented context for the capacity-profile.yaml artifact and
    for a human reading the experiment -- NOT enforced by this repo.
    Real RPM/TPM enforcement is Bedrock's own; this repo only ever
    measures what actually happens, it never simulates or caps against
    a quota number itself."""
    rpm: Optional[float] = None
    tpm: Optional[float] = None


@dataclass
class SloConfig:
    ttft_p95_ms: Optional[float] = None
    latency_p95_ms: Optional[float] = None


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
    quota: QuotaConfig = field(default_factory=QuotaConfig)
    slo: SloConfig = field(default_factory=SloConfig)
    duration_s: float = 60.0
    stream: bool = True
    provider_headroom: float = 0.20
    seed: Optional[int] = None
    # SLO-gate thresholds passed through to analysis.capacity.meets_slo
    # beyond the SLO latencies above -- kept separate since these are
    # about RESULT QUALITY (success/throttle rate), not response speed.
    success_rate_min: float = 0.99
    throttle_rate_max: float = 0.001


def load_experiment(path: str) -> ExperimentSpec:
    raw = yaml.safe_load(Path(path).read_text())

    target = raw["target"]
    quota = raw.get("quota") or {}
    slo = raw.get("slo") or {}
    sweep = raw["sweep"]
    workloads = [WorkloadProfile(**w) for w in raw["workloads"]]

    return ExperimentSpec(
        name=raw["name"],
        description=raw.get("description", ""),
        target=TargetConfig(**target),
        quota=QuotaConfig(**quota),
        slo=SloConfig(**slo),
        workloads=workloads,
        duration_s=raw.get("duration_s", 60.0),
        stream=raw.get("stream", True),
        sweep=SweepConfig(**sweep),
        provider_headroom=raw.get("provider_headroom", 0.20),
        seed=raw.get("seed"),
        success_rate_min=raw.get("success_rate_min", 0.99),
        throttle_rate_max=raw.get("throttle_rate_max", 0.001),
    )
