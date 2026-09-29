"""Constraints -- the two inputs every capacity number is judged against:

    constraints/
      slo.yaml                    SLO:    what quality we REQUIRE (named profiles per use case)
      quota.yaml                  quota:  what capacity the PROVIDER ALLOWS (per account/region/model)
      recommendation-policy.yaml  policy: headroom from a confirmed measurement to a recommendation

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

import math

import yaml

DEFAULT_SLO_FILE = "constraints/slo.yaml"
DEFAULT_QUOTA_FILE = "constraints/quota.yaml"
DEFAULT_POLICY_FILE = "constraints/recommendation-policy.yaml"


@dataclass
class RecommendationPolicy:
    """Policy, not measurement: the safety margins recommendation.py
    applies to a statistically confirmed point."""
    headroom_fraction: float = 0.20
    quota_headroom_fraction: float = 0.10


def load_policy(path: str = DEFAULT_POLICY_FILE) -> RecommendationPolicy:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    policy = RecommendationPolicy(**raw)
    for name in ("headroom_fraction", "quota_headroom_fraction"):
        if not 0 <= getattr(policy, name) < 1:
            raise ValueError(f"{path}: {name} must be in [0, 1)")
    return policy


@dataclass
class SloConfig:
    ttft_p95_ms: Optional[float] = None
    # Time per output token (decode speed), p95: (latency - TTFT) /
    # (output_tokens - 1) per streamed request. Unlike end-to-end
    # latency it doesn't grow with output length, so one number is
    # meaningful for a 64-token reply and a 1024-token generation alike.
    tpot_p95_ms: Optional[float] = None
    # End-to-end latency, p95 -- optional; TTFT + TPOT usually say more.
    latency_p95_ms: Optional[float] = None
    # Result-quality gates -- distinct from the two latencies above
    # (response SPEED), these are about whether responses came back at
    # all and cleanly.
    success_rate_min: float = 0.99
    throttle_rate_max: float = 0.001
    # Confidence for the success/throttle checks (default 0.95): judged
    # PASS / FAIL / INCONCLUSIVE on exact (Clopper-Pearson) one-sided
    # bounds -- a point has to have enough requests to DEMONSTRATE it
    # meets a 0.1% throttle SLO (2,995 at 95%), not just happen to
    # observe zero throttles in a few hundred. None = 0.95.
    confidence: Optional[float] = None


@dataclass(frozen=True)
class TtftBudget:
    # Inclusive upper bound; the final band has no upper bound.
    max_input_tokens: Optional[int]
    ttft_p95_ms: float


def ttft_budget_for(budgets: Dict[str, TtftBudget], input_tokens: int):
    for name, budget in budgets.items():
        if budget.max_input_tokens is None or input_tokens <= budget.max_input_tokens:
            return name, budget
    raise ValueError(f"no TTFT budget for {input_tokens} input tokens")


@dataclass
class SloProfiles:
    profiles: Dict[str, SloConfig] = field(default_factory=dict)
    ttft_budgets: Dict[str, TtftBudget] = field(default_factory=dict)

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
    budgets = {}
    if "ttft_budgets" in raw:
        configured = raw["ttft_budgets"]
        if not isinstance(configured, dict) or not configured:
            raise ValueError(f"{path}: ttft_budgets must be a nonempty mapping")
        previous = 0
        for index, (name, cfg) in enumerate(configured.items()):
            budget = TtftBudget(**cfg)
            upper = budget.max_input_tokens
            if upper is None:
                if index != len(configured) - 1:
                    raise ValueError("unbounded TTFT band must be last")
            elif type(upper) is not int or upper <= previous:
                raise ValueError("TTFT upper bounds must be strictly increasing positive integers")
            else:
                previous = upper
            if (isinstance(budget.ttft_p95_ms, bool) or
                    not isinstance(budget.ttft_p95_ms, (int, float)) or
                    not math.isfinite(budget.ttft_p95_ms) or budget.ttft_p95_ms <= 0):
                raise ValueError("TTFT budgets must be finite positive milliseconds")
            budgets[name] = budget
        if list(budgets.values())[-1].max_input_tokens is not None:
            raise ValueError("TTFT bands must end with max_input_tokens: null")
        if any(p.ttft_p95_ms is not None for p in profiles.values()):
            raise ValueError("use ttft_budgets or profile ttft_p95_ms, not both")
    return SloProfiles(profiles=profiles, ttft_budgets=budgets)


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
