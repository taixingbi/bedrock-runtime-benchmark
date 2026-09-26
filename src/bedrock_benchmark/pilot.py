"""Pilot run -- a cheap smoke test before a long batch. For every model x
workload the planned experiments would use, calibrate the padding and
send a few SEQUENTIAL requests (no load), then check:

  access    every request succeeds -- credentials, model access, region,
            request shape (an AccessDenied / ValidationException here
            would fail every run of the batch)
  shape     input within workload_validation_tolerance_pct of target, and
            output reaches its budget (stop_reason max_tokens, output p50
            within output_validation_tolerance_pct) -- otherwise the batch
            measures a different workload than it claims
  slo       unloaded TTFT / TPOT / E2E p50 already within the workload's
            SLO -- if a single request with no load misses it, no load
            level can PASS, and the sweep would only spend time proving that

access and shape problems are FAIL (the batch's results would be wrong
or empty); an unreachable SLO is WARN (the batch still runs, and
correctly reports no confirmed point). Pilot requests are never mixed
into any experiment's data.
"""
from __future__ import annotations

import asyncio
import statistics
from collections import Counter
from dataclasses import dataclass, field, replace
from typing import Callable, Collection, Dict, List, Optional

from .analysis.metrics import tpot_ms
from .client import BedrockConverseTarget, InvokeRequest
from .constraints import DEFAULT_SLO_FILE, SloConfig
from .experiments.executor import calibrate_workloads
from .experiments.schema import ExperimentSpec, NoMatchingWorkloads, load_experiment
from .models import ModelConfig
from .results import RequestResult
from .workload import DEFAULT_WORKLOADS_FILE, WorkloadProfile

TargetFactory = Callable[[ExperimentSpec], BedrockConverseTarget]
OK, WARN, FAIL = "OK", "WARN", "FAIL"


@dataclass
class PilotCheck:
    model: str
    workload: str
    slo_profile: str
    status: str                       # OK | WARN | FAIL
    issues: List[str] = field(default_factory=list)
    requests: int = 0
    errors: Dict[str, int] = field(default_factory=dict)
    calibration: Optional[str] = None
    input_target: int = 0
    input_p50: Optional[float] = None
    output_target: int = 0
    output_p50: Optional[float] = None
    max_tokens_share: Optional[float] = None
    ttft_p50_ms: Optional[float] = None
    tpot_p50_ms: Optional[float] = None
    latency_p50_ms: Optional[float] = None

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v not in (None, [], {})}


@dataclass
class PilotReport:
    checks: List[PilotCheck] = field(default_factory=list)

    @property
    def failed(self) -> List[PilotCheck]:
        return [c for c in self.checks if c.status == FAIL]

    @property
    def exit_code(self) -> int:
        return 1 if self.failed else 0

    def to_dict(self) -> dict:
        return {"checks": [c.to_dict() for c in self.checks],
                "summary": dict(Counter(c.status for c in self.checks))}


def pilot_workloads(
    paths: List[str], model: ModelConfig, *, slo_file: str = DEFAULT_SLO_FILE,
    workloads_file: str = DEFAULT_WORKLOADS_FILE, only_slo_profiles: Optional[Collection[str]] = None,
) -> Optional[ExperimentSpec]:
    """One spec for `model` whose workloads are the union of what the
    planned experiments would send (after any --slo-profile filter), in
    first-seen order -- None if nothing would run."""
    base, seen = None, {}
    for path in paths:
        try:
            spec = load_experiment(path, model, slo_file=slo_file, workloads_file=workloads_file,
                                   only_slo_profiles=only_slo_profiles)
        except NoMatchingWorkloads:
            continue
        base = base or spec
        for w in spec.workloads:
            seen.setdefault(w.name, w)
    if base is None:
        return None
    return replace(base, workloads=list(seen.values()), mix=None)


def _p50(values) -> Optional[float]:
    values = [v for v in values if v is not None]
    return round(statistics.median(values), 2) if values else None


def evaluate(model: str, workload: WorkloadProfile, slo: SloConfig, results: List[RequestResult],
             spec: ExperimentSpec, calibration: Optional[str]) -> PilotCheck:
    ok = [r for r in results if r.success]
    check = PilotCheck(
        model=model, workload=workload.name, slo_profile=workload.slo_profile or "", status=OK,
        requests=len(results), calibration=calibration,
        errors=dict(Counter(r.error_code or "error" for r in results if not r.success)),
        input_target=workload.input_tokens, output_target=workload.output_tokens,
        input_p50=_p50(r.input_tokens for r in ok), output_p50=_p50(r.output_tokens for r in ok),
        max_tokens_share=round(sum(r.stop_reason == "max_tokens" for r in ok) / len(ok), 3) if ok else None,
        ttft_p50_ms=_p50(r.ttft_ms for r in ok), tpot_p50_ms=_p50(tpot_ms(r) for r in ok),
        latency_p50_ms=_p50(r.latency_ms for r in ok),
    )
    fail, warn = [], []
    # access
    if check.errors:
        throttles = check.errors.get("ThrottlingException", 0)
        others = sum(check.errors.values()) - throttles
        if others:
            fail.append(f"access: {others}/{len(results)} requests failed {check.errors}")
        if throttles:
            warn.append(f"throttled {throttles}x at 1 request at a time -- something else is using this quota")
    if not ok:
        fail.append("access: no successful request")
    else:
        # shape
        dev_in = abs(check.input_p50 - workload.input_tokens) / workload.input_tokens * 100
        if dev_in > spec.workload_validation_tolerance_pct:
            fail.append(f"shape: input p50 {check.input_p50:.0f} vs {workload.input_tokens} "
                        f"({dev_in:.1f}% > {spec.workload_validation_tolerance_pct}%)")
        dev_out = abs(check.output_p50 - workload.output_tokens) / workload.output_tokens * 100
        if dev_out > spec.output_validation_tolerance_pct:
            fail.append(f"shape: output p50 {check.output_p50:.0f} vs {workload.output_tokens} "
                        f"({dev_out:.1f}% > {spec.output_validation_tolerance_pct}%; "
                        f"{check.max_tokens_share:.0%} ended on max_tokens)")
        # SLO reachable at all (unloaded)
        for label, observed, limit in (("TTFT", check.ttft_p50_ms, slo.ttft_p95_ms),
                                       ("TPOT", check.tpot_p50_ms, slo.tpot_p95_ms),
                                       ("E2E", check.latency_p50_ms, slo.latency_p95_ms)):
            if limit is None:
                continue
            if observed is None:
                warn.append(f"slo: {label} not measured (streaming off or < 2 output tokens) -- "
                            f"its p95 check will FAIL closed")
            elif observed > limit:
                warn.append(f"slo: unloaded {label} p50 {observed:.1f}ms already exceeds the p95 limit "
                            f"{limit:.0f}ms -- no load level can PASS")
    check.issues = fail + warn
    check.status = FAIL if fail else (WARN if warn else OK)
    return check


async def run_pilot(
    paths: List[str], models: List[ModelConfig], *, requests_per_workload: int = 3,
    slo_file: str = DEFAULT_SLO_FILE, workloads_file: str = DEFAULT_WORKLOADS_FILE,
    only_slo_profiles: Optional[Collection[str]] = None, target_factory: Optional[TargetFactory] = None,
    on_check: Optional[Callable[[PilotCheck], None]] = None,
) -> PilotReport:
    report = PilotReport()
    for model in models:
        spec = pilot_workloads(paths, model, slo_file=slo_file, workloads_file=workloads_file,
                               only_slo_profiles=only_slo_profiles)
        if spec is None:
            continue
        target = target_factory(spec) if target_factory else BedrockConverseTarget(
            model_id=model.model_id, region=model.region, transport=spec.transport)
        try:
            calibrations = calibrate_workloads(spec, target)
            for workload in spec.workloads:
                cal = calibrations[workload.name]
                shaped = cal.profile
                results = [
                    await target.invoke(InvokeRequest(prompt=shaped.prompt(), max_tokens=shaped.output_tokens,
                                                      stream=spec.stream))
                    for _ in range(requests_per_workload)
                ]
                check = evaluate(model.name, workload, spec.slo_for(workload.name), results, spec,
                                 calibration=f"{cal.method}" + ("" if cal.converged or cal.method == "estimate"
                                                                else " (not converged)"))
                report.checks.append(check)
                if on_check is not None:
                    on_check(check)
        finally:
            if target_factory is None:
                target.close()
    return report


def format_check(c: PilotCheck) -> str:
    def f(v, unit=""):
        return "-" if v is None else f"{v:.0f}{unit}"
    line = (f"{c.status:<5} {c.model:<14} {c.workload:<27} {c.slo_profile:<7} "
            f"in {f(c.input_p50)}/{c.input_target:<6} out {f(c.output_p50)}/{c.output_target:<5} "
            f"ttft {f(c.ttft_p50_ms, 'ms'):<7} tpot {'-' if c.tpot_p50_ms is None else f'{c.tpot_p50_ms:.1f}ms':<7} "
            f"e2e {f(c.latency_p50_ms, 'ms')}")
    return line + "".join(f"\n      - {issue}" for issue in c.issues)


def run_pilot_sync(*args, **kwargs) -> PilotReport:
    return asyncio.run(run_pilot(*args, **kwargs))
