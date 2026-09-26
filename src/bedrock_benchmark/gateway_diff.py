"""Gateway recommendation diff -- compares one or more measured
capacity-profile.yaml artifacts against a SNAPSHOT of
bedrock-runtime-gateway's current limits, and lists where the gateway
admits more than the measured envelope says is safe.

Advisory and read-only: it never writes gateway config, and it only
proposes a value where a profile field maps directly onto a gateway
knob. Everything else is reported as context, not as a proposal --
the gateway's own config review still decides policy (see this repo's
README, "Not in scope here").

Gateway knobs compared (names as bedrock-runtime-gateway uses them):

- models.<id>.rpm_limit -- gateway-model-quotas-dev `quota#<model_id>`
  row, enforced in routing/model_quota.py. Compared to the tightest
  `production_sustained_rps * 60` (v3-v5: production_offered_rps) across the model's measured classes
  and mixes: the gateway's model limit is workload-agnostic, so the
  binding class is the conservative bound.
- tenants.<name>.rpm_limit -- tenant policy. A single tenant above the
  model envelope is flagged; the sum across tenants is context only
  (oversubscription can be deliberate).
- concurrency.default_tenant_max -- CONCURRENCY_DEFAULT_TENANT_MAX.
  A single tenant allowed more in-flight calls than one model's
  measured production_max can push that model past its knee alone.
- concurrency.global_max (x processes) -- CONCURRENCY_GLOBAL_MAX spans
  every model on a process, so exceeding one model's envelope is
  context, not a proposal.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

SUPPORTED_SCHEMA_VERSIONS = {3, 4, 5, 6}


@dataclass
class Finding:
    severity: str  # "warn" | "info"
    kind: str
    model_id: Optional[str]
    message: str
    current: Optional[float] = None
    proposed: Optional[float] = None
    basis: Optional[str] = None  # which class/mix and profile field the number comes from


@dataclass
class GatewayDiff:
    findings: List[Finding] = field(default_factory=list)

    @property
    def proposed_changes(self) -> List[Finding]:
        return [f for f in self.findings if f.proposed is not None]

    def to_dict(self) -> dict:
        return {
            "summary": {
                "warn": sum(1 for f in self.findings if f.severity == "warn"),
                "info": sum(1 for f in self.findings if f.severity == "info"),
                "proposed_changes": len(self.proposed_changes),
            },
            "findings": [{k: v for k, v in asdict(f).items() if v is not None} for f in self.findings],
        }


def _envelopes(profile: dict) -> Tuple[List[Tuple[str, float]], List[Tuple[str, int]]]:
    """(rate envelopes as (basis, production rps), concurrency
    envelopes as (basis, production_max)) across isolated classes and
    mixes in one profile."""
    rates: List[Tuple[str, float]] = []
    concs: List[Tuple[str, int]] = []
    sources = [("class", n, e) for n, e in (profile.get("workload_classes") or {}).items()]
    sources += [("mix", n, e) for n, e in (profile.get("mixed_workloads") or {}).items()]
    for kind, name, entry in sources:
        if "rate" in entry:
            # v6: production_sustained_rps (capped by the provider ceiling);
            # v3-v5: production_offered_rps.
            key = "production_sustained_rps" if "production_sustained_rps" in entry["rate"] else "production_offered_rps"
            rates.append((f"{kind} {name}: rate.{key}", entry["rate"][key]))
        if "concurrency" in entry:
            concs.append((f"{kind} {name}: concurrency.production_max", entry["concurrency"]["production_max"]))
    return rates, concs


def _entries(profile: dict):
    yield from (("class", n, e) for n, e in (profile.get("workload_classes") or {}).items())
    yield from (("mix", n, e) for n, e in (profile.get("mixed_workloads") or {}).items())


def _quality_findings(model_id: str, profile: dict) -> Iterable[Finding]:
    for name, entry in (profile.get("workload_classes") or {}).items():
        validation = entry.get("workload_validation") or {}
        if validation.get("valid") is not False:
            continue
        # v4 nests input/output checks; v3 had flat input-only fields.
        sides = [(k, validation[k]) for k in ("input", "output") if isinstance(validation.get(k), dict)]
        if not sides:
            sides = [("input", {"valid": False, "observed_p50": validation.get("observed_input_tokens_p50"),
                                "target": validation.get("requested_input_tokens"),
                                "deviation_pct": validation.get("deviation_pct")})]
        detail = "; ".join(
            f"{side} p50 {c.get('observed_p50')} vs {c.get('target')} ({c.get('deviation_pct')}%)"
            for side, c in sides if c.get("valid") is False
        )
        yield Finding(
            "warn", "workload_shape_invalid", model_id,
            f"class {name} measured {detail} -- its envelope describes a different workload than it claims",
        )

    # v6: each envelope carries its verdict; INCONCLUSIVE means nothing
    # violated the SLO but there weren't enough requests to prove it.
    for kind, name, entry in _entries(profile):
        block = entry.get("rate") or entry.get("concurrency") or {}
        if block.get("verdict") == "INCONCLUSIVE":
            detail = "; ".join(
                f"{c.get('name')}: n={c.get('n')} < {c.get('required_n')}" for c in block.get("inconclusive_checks", [])
            )
            yield Finding(
                "info", "envelope_unconfirmed", model_id,
                f"{kind} {name}: safe point is INCONCLUSIVE (no violation, too few requests to prove the SLO: {detail})"
                + ("" if block.get("confirmed_safe") is None else f"; statistically confirmed safe: {block['confirmed_safe']}"),
            )

    measurement = profile.get("measurement") or {}
    needed = measurement.get("min_requests_to_resolve_throttle_slo")
    if measurement.get("gate") != "point_estimate" or not needed:
        return
    sources = [("class", n, e) for n, e in (profile.get("workload_classes") or {}).items()]
    sources += [("mix", n, e) for n, e in (profile.get("mixed_workloads") or {}).items()]
    for kind, name, entry in sources:
        evidence = entry.get("evidence") or {}
        if evidence.get("n") is not None and evidence["n"] < needed:
            yield Finding(
                "info", "throttle_slo_unresolved", model_id,
                f"{kind} {name} passed on {evidence['n']} requests; resolving throttle_rate_max needs >= {needed} "
                f"(throttle upper bound {evidence.get('throttle_rate_upper')})",
            )


def diff(profiles: List[dict], gateway: dict) -> GatewayDiff:
    out = GatewayDiff()
    by_model: Dict[str, List[dict]] = {}
    for profile in profiles:
        version = profile.get("schema_version")
        if version not in SUPPORTED_SCHEMA_VERSIONS:
            raise ValueError(f"capacity profile schema_version {version} unsupported (need {sorted(SUPPORTED_SCHEMA_VERSIONS)})")
        by_model.setdefault(profile["model"]["model_id"], []).append(profile)

    gw_models = gateway.get("models") or {}
    gw_tenants = gateway.get("tenants") or {}
    gw_conc = gateway.get("concurrency") or {}

    for model_id, model_profiles in by_model.items():
        rates: List[Tuple[str, float]] = []
        concs: List[Tuple[str, int]] = []
        for profile in model_profiles:
            r, c = _envelopes(profile)
            rates += r
            concs += c
            out.findings.extend(_quality_findings(model_id, profile))

        tenants_on_model = {n: t for n, t in gw_tenants.items() if model_id in (t.get("models") or [])}

        if rates:
            basis, safe_rps = min(rates, key=lambda x: x[1])
            # Round before flooring: rps values are stored to 4 decimals, so
            # 1.0x of a 400 RPM quota comes back as 6.6666 rps -> 399.996
            # rpm, and a bare floor would propose a spurious 400 -> 399 cut.
            safe_rpm = math.floor(round(safe_rps * 60, 1))
            current = (gw_models.get(model_id) or {}).get("rpm_limit")
            if current is None:
                out.findings.append(Finding(
                    "warn", "model_rpm_unset", model_id,
                    f"gateway has no rpm_limit for this model (model-quota gate fails open); measured envelope is {safe_rpm} rpm",
                    proposed=safe_rpm, basis=basis,
                ))
            elif current > safe_rpm:
                out.findings.append(Finding(
                    "warn", "model_rpm_above_envelope", model_id,
                    f"gateway admits {current} rpm; measured production envelope is {safe_rpm} rpm",
                    current=current, proposed=safe_rpm, basis=basis,
                ))
            else:
                out.findings.append(Finding(
                    "info", "model_rpm_within_envelope", model_id,
                    f"gateway rpm_limit {current} <= measured envelope {safe_rpm}", current=current, basis=basis,
                ))

            for tenant, policy in tenants_on_model.items():
                t_rpm = policy.get("rpm_limit")
                if t_rpm is not None and t_rpm > safe_rpm:
                    out.findings.append(Finding(
                        "warn", "tenant_rpm_above_envelope", model_id,
                        f"tenant {tenant} alone may send {t_rpm} rpm to this model; envelope is {safe_rpm} rpm",
                        current=t_rpm, proposed=safe_rpm, basis=basis,
                    ))
            total = sum(p.get("rpm_limit") or 0 for p in tenants_on_model.values())
            if total > safe_rpm:
                out.findings.append(Finding(
                    "info", "tenant_rpm_oversubscribed", model_id,
                    f"tenants allowed on this model sum to {total} rpm vs {safe_rpm} rpm envelope "
                    f"({', '.join(sorted(tenants_on_model))}) -- fine only if they don't peak together",
                    current=total, basis=basis,
                ))

        if concs:
            basis, safe_conc = min(concs, key=lambda x: x[1])
            tenant_max = gw_conc.get("default_tenant_max")
            if tenant_max is not None and tenant_max > safe_conc:
                out.findings.append(Finding(
                    "warn", "tenant_concurrency_above_model_envelope", model_id,
                    f"CONCURRENCY_DEFAULT_TENANT_MAX={tenant_max} lets one tenant exceed this model's "
                    f"measured production_max={safe_conc}",
                    current=tenant_max, proposed=safe_conc, basis=basis,
                ))
            global_max = gw_conc.get("global_max")
            if global_max is not None:
                effective = global_max * (gw_conc.get("processes") or 1)
                if effective > safe_conc:
                    out.findings.append(Finding(
                        "info", "global_concurrency_exceeds_single_model_envelope", model_id,
                        f"global concurrency {effective} (global_max x processes) spans all models; "
                        f"this model alone is safe to {safe_conc} -- a risk only if traffic concentrates on it",
                        current=effective, basis=basis,
                    ))

        if not rates and not concs:
            out.findings.append(Finding(
                "info", "no_envelope", model_id, "no swept value met the SLO in any profile for this model -- nothing to compare",
            ))

    severity_order = {"warn": 0, "info": 1}
    out.findings.sort(key=lambda f: (severity_order.get(f.severity, 2), f.model_id or "", f.kind))
    return out
