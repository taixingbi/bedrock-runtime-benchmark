"""Constraints -- the two inputs every capacity number is judged against:

    constraints/
      slo.yaml     SLO:   what quality we REQUIRE (named profiles per use case)
      quota.yaml   quota: what capacity the PROVIDER ALLOWS (per account/region/model)

Both are defined once, outside experiments and outside the models
list, so every experiment x model run is judged by the same SLO for the
same workload class and swept against the same quota. Experiments carry
only workloads and sweeps; the loaders reject SLO or quota numbers
anywhere else, so a copy can't drift.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

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
    profiles: Dict[str, SloConfig] = field(default_factory=dict)

    def get(self, name: str) -> SloConfig:
        return self.profiles[name]


@dataclass
class Quota:
    rpm: Optional[float] = None
    tpm: Optional[float] = None
    # How many TPM-quota tokens one output token costs (some models bill
    # output at a multiple against TPM). See ceiling.py.
    output_burndown: float = 1.0


# (account id, region, model name) -> Quota
QuotaTable = Dict[Tuple[str, str, str], Quota]


def load_slo(path: str = DEFAULT_SLO_FILE) -> SloProfiles:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    if "default" in raw:
        raise ValueError(f"{path}: no `default:` -- every workload names its slo_profile explicitly")
    profiles = {name: SloConfig(**(cfg or {})) for name, cfg in (raw.get("profiles") or {}).items()}
    if not profiles:
        raise ValueError(f"{path}: needs at least one entry under `profiles:`")
    for name, p in profiles.items():
        if p.confidence is not None and not 0 < p.confidence < 1:
            raise ValueError(f"{path}: profiles.{name}.confidence must be in (0, 1)")
        if not 0 <= p.throttle_rate_max <= 1 or not 0 <= p.success_rate_min <= 1:
            raise ValueError(f"{path}: profiles.{name} rates must be in [0, 1]")
    return SloProfiles(profiles=profiles)


def load_quotas(path: str = DEFAULT_QUOTA_FILE) -> QuotaTable:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    accounts = raw.get("accounts")
    if not isinstance(accounts, dict) or not accounts:
        raise ValueError(f"{path}: expected `accounts: {{<account id>: {{<region>: {{<model>: {{rpm, tpm}}}}}}}}`")
    table: QuotaTable = {}
    for account, regions in accounts.items():
        account = str(account)
        if not account.isdigit() or len(account) != 12:
            raise ValueError(f"{path}: account {account!r} must be a 12-digit AWS account id (quote it in YAML)")
        for region, models in (regions or {}).items():
            for name, cfg in (models or {}).items():
                quota = Quota(**(cfg or {}))
                if quota.output_burndown <= 0:
                    raise ValueError(f"{path}: {account}/{region}/{name}.output_burndown must be > 0")
                table[(account, region, name)] = quota
    return table


def quota_accounts(table: QuotaTable) -> List[str]:
    return sorted({account for account, _, _ in table})


def resolve_account(table: QuotaTable, requested: Optional[str], *, path: str = DEFAULT_QUOTA_FILE) -> str:
    """The account whose quotas apply. `requested` is the live account
    (STS) or an explicit --account. With neither, a file holding exactly
    one account is used as-is (offline dry runs, CI); otherwise it's an
    error -- never guess which account's quota a sweep is built on."""
    accounts = quota_accounts(table)
    if requested is not None:
        requested = str(requested)
        if requested not in accounts:
            raise ValueError(
                f"{path} has no quotas for AWS account {requested} (has: {accounts}) -- add them "
                f"(scripts/fetch_quota.py --all prints the lines) rather than sweeping around another account's quota"
            )
        return requested
    if len(accounts) == 1:
        return accounts[0]
    raise ValueError(f"{path} lists several accounts {accounts}; pass --account or run with AWS credentials")


def current_account_id() -> Optional[str]:
    """The live AWS account from STS, or None without usable credentials
    (offline dry runs still work against a single-account quota file)."""
    try:
        import boto3

        return str(boto3.client("sts").get_caller_identity()["Account"])
    except Exception:  # noqa: BLE001 - no creds / no network: caller falls back
        return None
