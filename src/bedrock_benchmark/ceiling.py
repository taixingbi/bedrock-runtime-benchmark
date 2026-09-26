"""Provider ceiling -- the theoretical max request rate a model's quota
allows for a given workload, before anything is measured:

    rpm_rps = RPM / 60
    tpm_rps = TPM / tokens_per_request / 60
    ceiling = min(rpm_rps, tpm_rps)      # whichever binds first

Quotas cap BOTH requests and tokens, and which one binds depends on the
workload: 512 in / 64 out on a 400 RPM / 8M TPM model is RPM-bound
(6.67 rps vs ~230 rps), but on an 80 RPM / 600k TPM model 4096 in /
512 out is TPM-bound (1.33 vs ~2.2 rps -- close), and a heavier shape
flips it. Quota-relative rate sweeps are fractions of THIS ceiling, not
of RPM alone, so a long workload is swept around the limit it actually
hits.

tokens_per_request counts input + max_tokens x output_burndown: Bedrock
reserves input + max_tokens against the TPM quota when a request starts
(adjusted to actual usage at completion), so max_tokens -- not the
eventual output -- is what throttling decides on. output_burndown
(models file, default 1) covers models whose output tokens count more
than once against TPM.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

from .workload import WorkloadProfile


@dataclass
class ProviderCeiling:
    tokens_per_request: float
    rpm_rps: Optional[float]
    tpm_rps: Optional[float]

    @property
    def rps(self) -> Optional[float]:
        known = [v for v in (self.rpm_rps, self.tpm_rps) if v is not None]
        return min(known) if known else None

    @property
    def binding(self) -> Optional[str]:
        if self.rpm_rps is None and self.tpm_rps is None:
            return None
        if self.tpm_rps is None or (self.rpm_rps is not None and self.rpm_rps <= self.tpm_rps):
            return "rpm"
        return "tpm"

    def to_dict(self) -> dict:
        return {
            "tokens_per_request": round(self.tokens_per_request, 1),
            "rpm_rps_ceiling": None if self.rpm_rps is None else round(self.rpm_rps, 4),
            "tpm_rps_ceiling": None if self.tpm_rps is None else round(self.tpm_rps, 4),
            "ceiling_rps": None if self.rps is None else round(self.rps, 4),
            "binding_constraint": self.binding,
        }


def provider_ceiling(
    classes: List[Tuple[WorkloadProfile, float]], *, rpm: Optional[float], tpm: Optional[float],
    output_burndown: float = 1.0,
) -> ProviderCeiling:
    """classes: (workload, share) -- one entry with share 1.0 for an
    isolated workload, the normalized mix shares for a WorkloadMix."""
    total = sum(share for _, share in classes)
    tokens = sum(share / total * (w.input_tokens + w.output_tokens * output_burndown) for w, share in classes)
    return ProviderCeiling(
        tokens_per_request=tokens,
        rpm_rps=rpm / 60.0 if rpm else None,
        tpm_rps=tpm / tokens / 60.0 if tpm and tokens > 0 else None,
    )
