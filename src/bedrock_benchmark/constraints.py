"""Constraints -- the two inputs every capacity number is judged against:

    constraints/
      slo.yaml     SLO:   what quality we REQUIRE (per traffic class)
      quota.yaml   quota: what capacity the PROVIDER ALLOWS (per model)

Both are defined once, outside experiments and outside the models
list, so every experiment x model run is judged by the same SLO for the
same workload class and swept against the same quota. Experiments carry
only workloads and sweeps; the loaders reject SLO or quota numbers
anywhere else, so a copy can't drift.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

import yaml

DEFAULT_SLO_FILE = "constraints/slo.yaml"
DEFAULT_QUOTA_FILE = "constraints/quota.yaml"


@dataclass
class SloConfig:
    ttft_p95_ms: Optional[float] = None
    latency_p95_ms: Optional[float] = None
    # Result-quality gates -- distinct from the two latencies above
    # (response SPEED), these are about whether responses came back at
    # all and cleanly.
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
class SloProfiles:
    default: str
    profiles: Dict[str, SloConfig] = field(default_factory=dict)

    def get(self, name: Optional[str]) -> SloConfig:
        return self.profiles[name or self.default]


@dataclass
class Quota:
    rpm: Optional[float] = None
    tpm: Optional[float] = None
    # How many TPM-quota tokens one output token costs (some models bill
    # output at a multiple against TPM). See ceiling.py.
    output_burndown: float = 1.0


def load_slo(path: str = DEFAULT_SLO_FILE) -> SloProfiles:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    profiles = {name: SloConfig(**(cfg or {})) for name, cfg in (raw.get("profiles") or {}).items()}
    if not profiles:
        raise ValueError(f"{path}: needs at least one entry under `profiles:`")
    default = raw.get("default")
    if default not in profiles:
        raise ValueError(f"{path}: `default: {default}` must name one of the profiles {sorted(profiles)}")
    for name, p in profiles.items():
        if p.confidence is not None and not 0 < p.confidence < 1:
            raise ValueError(f"{path}: profiles.{name}.confidence must be in (0, 1)")
        if not 0 <= p.throttle_rate_max <= 1 or not 0 <= p.success_rate_min <= 1:
            raise ValueError(f"{path}: profiles.{name} rates must be in [0, 1]")
    return SloProfiles(default=default, profiles=profiles)


def load_quotas(path: str = DEFAULT_QUOTA_FILE) -> Dict[str, Quota]:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    quotas = {}
    for name, cfg in raw.items():
        quota = Quota(**(cfg or {}))
        if quota.output_burndown <= 0:
            raise ValueError(f"{path}: {name}.output_burndown must be > 0")
        quotas[name] = quota
    return quotas
