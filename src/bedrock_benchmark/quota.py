"""fetch_quota_snapshot -- gets a model's real RPM/TPM quota to help
DESIGN an experiment (e.g. picking sane rate-sweep values), never to
enforce anything at runtime -- this repo's own README is explicit that
quota_snapshot in an experiment YAML is documented context, not
something this tool checks or caps against.

Two sources, tried in order, so this repo's stated "zero coupling to
bedrock-runtime-gateway's infrastructure" principle stays mostly true
while still being practical:

1. gateway-model-quotas-dev's "quota#<model_id>" row -- if that table
   happens to be reachable (the same AWS account/region gateway is
   deployed to) and already has a synced row, this is one cheap
   GetItem instead of a full AWS Service Quotas listing call. A soft
   convenience, not a hard dependency: this repo doesn't provision
   that table, doesn't assume it exists, and every field it reads is
   itself just a cache of AWS's own published quota (see
   bedrock-runtime-gateway's own scripts/sync_model_quotas_from_aws.py,
   which is where that row ultimately comes from).
2. AWS Service Quotas directly (service-quotas:ListServiceQuotas) --
   the actual source of truth, queried the exact same way that sync
   script does (same model-id-to-quota-display-name mapping,
   duplicated here rather than imported: this repo never imports
   bedrock-runtime-gateway's code, see client.py's own docstring on
   that boundary). Used whenever the table lookup fails for ANY reason
   (table missing, row missing, no permission, wrong account) -- never
   a hard error, since the whole point is "give me the best available
   number to plan an experiment with," not "assert this table is
   reachable."
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

# model_id -> (Service Quotas display name, is_cross_region) -- see
# bedrock-runtime-gateway's scripts/sync_model_quotas_from_aws.py for
# the original, authoritative copy of this mapping and why it has to
# be hand-maintained (no AWS API maps a model_id to this display name
# directly). Duplicated, not imported, per this repo's own boundary.
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
    source: str  # "table" | "service_quotas" | "unknown" -- which path actually produced this


def _from_table(model_id: str, *, region: str, table_name: str, dynamo_client: Optional[Any]) -> Optional[QuotaInfo]:
    try:
        if dynamo_client is None:
            import boto3
            dynamo_client = boto3.client("dynamodb", region_name=region)
        response = dynamo_client.get_item(TableName=table_name, Key={"pk": {"S": f"quota#{model_id}"}})
    except Exception:  # noqa: BLE001 - table missing/unreachable/no permission all fall through to Service Quotas
        return None

    item = response.get("Item")
    if item is None or "rpm_limit" not in item:
        return None
    return QuotaInfo(
        rpm=float(item["rpm_limit"]["N"]),
        tpm=float(item["tpm_limit"]["N"]) if "tpm_limit" in item else None,
        source="table",
    )


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


def fetch_quota_snapshot(
    model_id: str, *, region: str = "us-east-1", table_name: str = "gateway-model-quotas-dev",
    dynamo_client: Optional[Any] = None, sq_client: Optional[Any] = None,
) -> QuotaInfo:
    """Table first, then AWS Service Quotas directly, then "unknown"
    (rpm=None, tpm=None) rather than raising -- a missing quota number
    should never block an experiment design conversation, it should
    just make the gap visible."""
    result = _from_table(model_id, region=region, table_name=table_name, dynamo_client=dynamo_client)
    if result is not None:
        return result
    result = _from_service_quotas(model_id, region=region, sq_client=sq_client)
    if result is not None:
        return result
    return QuotaInfo(rpm=None, tpm=None, source="unknown")
