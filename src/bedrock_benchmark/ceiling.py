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

TPM pressure per request has two distinct parts (AWS Bedrock quota
docs):

  reservation   input + max_tokens -- deducted from the TPM quota when
                the request is ADMITTED; the burndown rate is NOT applied
  consumption   input + actual_output x output_burndown -- what the
                deduction is adjusted to when the request COMPLETES

Reservations are held while requests are in flight and consumption is
what settles against the minute, so tokens_per_request (the TPM
ceiling's denominator) is the LARGER of the two -- conservative either
way. The prompts are built to produce the full output budget
(stop_reason max_tokens, see workload.py), so expected actual_output is
max_tokens. With burndown 1 (Nova / Llama / Qwen) both are input +
max_tokens; with burndown 5 (some Claude models), 4k in / 1k out
reserves 5k but consumes 9k. output_burndown comes from
constraints/quota.yaml (default 1).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

from .workload import WorkloadProfile


@dataclass
class ProviderCeiling:
    tokens_per_request: float  # max(reservation, consumption) -- the TPM denominator
    rpm_rps: Optional[float]
    tpm_rps: Optional[float]
    reservation_tokens: Optional[float] = None   # input + max_tokens (at admission)
    consumption_tokens: Optional[float] = None   # input + expected_output x burndown (at completion)

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
            **({"reservation_tokens": round(self.reservation_tokens, 1),
                "consumption_tokens": round(self.consumption_tokens, 1),
                "token_pressure": "consumption" if self.consumption_tokens > self.reservation_tokens else "reservation"}
               if self.reservation_tokens is not None and self.consumption_tokens is not None else {}),
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
    # Expected actual output = max_tokens: the prompts elicit the full budget.
    reservation = sum(share / total * (w.input_tokens + w.output_tokens) for w, share in classes)
    consumption = sum(share / total * (w.input_tokens + w.output_tokens * output_burndown) for w, share in classes)
    tokens = max(reservation, consumption)
    return ProviderCeiling(
        tokens_per_request=tokens, reservation_tokens=reservation, consumption_tokens=consumption,
        rpm_rps=rpm / 60.0 if rpm else None,
        tpm_rps=tpm / tokens / 60.0 if tpm and tokens > 0 else None,
    )
