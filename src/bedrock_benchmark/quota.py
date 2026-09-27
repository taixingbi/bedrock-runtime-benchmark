"""fetch_quota_snapshot -- gets a model's real RPM/TPM quota to help
DESIGN an experiment (and to check constraints/quota.yaml is current),
never to enforce anything at runtime.

One source: AWS Service Quotas (service-quotas:ListServiceQuotas), the
provider's own source of truth. The benchmark reads nothing from any
gateway's infrastructure -- no tables, no config: it depends only on
Bedrock and AWS, and a consumer (e.g. bedrock-runtime-gateway) is never
a dependency. If Service Quotas can't answer (no permission, unmapped
model), the result is `unknown` rather than an error -- a missing quota
number should make the gap visible, not block experiment design.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

# model_id -> (Service Quotas display name, is_cross_region). Hand-
# maintained: no AWS API maps a model_id to its quota display name.
_MODEL_QUOTA_NAMES = {
    "us.amazon.nova-micro-v1:0": ("Amazon Nova Micro", True),
    "us.amazon.nova-lite-v1:0": ("Amazon Nova Lite", True),
    "us.amazon.nova-pro-v1:0": ("Amazon Nova Pro", True),
    "us.meta.llama3-3-70b-instruct-v1:0": ("Meta Llama 3.3 70B Instruct", True),
    "qwen.qwen3-32b-v1:0": ("Qwen3 32B V1", False),
}


@dataclass
class QuotaInfo:
    rpm: Optional[float]
    tpm: Optional[float]
    source: str  # "service_quotas" | "unknown"


def _from_service_quotas(model_id: str, *, region: str, sq_client: Optional[Any]) -> Optional[QuotaInfo]:
    mapping = _MODEL_QUOTA_NAMES.get(model_id)
    if mapping is None:
        return None
    display_name, is_cross_region = mapping
    kind = "Cross-region" if is_cross_region else "On-demand"

    try:
        if sq_client is None:
            import boto3
            sq_client = boto3.client("service-quotas", region_name=region)
        quotas = []
        paginator = sq_client.get_paginator("list_service_quotas")
        for page in paginator.paginate(ServiceCode="bedrock"):
            quotas.extend(page["Quotas"])
    except Exception:  # noqa: BLE001 - no permission/network issue -- caller gets None, not a crash
        return None

    by_name = {q["QuotaName"]: q["Value"] for q in quotas}
    rpm = by_name.get(f"{kind} model inference requests per minute for {display_name}")
    tpm = by_name.get(f"{kind} model inference tokens per minute for {display_name}")
    if rpm is None:
        return None
    return QuotaInfo(rpm=float(rpm), tpm=float(tpm) if tpm is not None else None, source="service_quotas")


def fetch_quota_snapshot(model_id: str, *, region: str = "us-east-1", sq_client: Optional[Any] = None) -> QuotaInfo:
    """AWS Service Quotas, else "unknown" (rpm=None, tpm=None) rather
    than raising."""
    result = _from_service_quotas(model_id, region=region, sq_client=sq_client)
    return result if result is not None else QuotaInfo(rpm=None, tpm=None, source="unknown")
