"""Paired open-loop observations under controlled recent traffic histories.

Time bins are descriptive, never additional hypothesis tests or capacity claims.
The observation is one continuous load window; bins do not restart the runner.
"""
from __future__ import annotations

import math

from ..analysis.capacity import SweepPoint
from ..analysis.observability import describe_metrics
from ..analysis.metrics import MeasurementWindow, compute_run_metrics
from ..runners.rate import RateRunner


def describe_window(rows, window, slo, offered_rps):
    m = compute_run_metrics(rows, windows=[window], offered_rps=offered_rps,
                            ttft_slo_ms=slo.ttft_p95_ms, tpot_slo_ms=slo.tpot_p95_ms,
                            latency_slo_ms=slo.latency_p95_ms)
    cohort = [r for r in rows if window.contains(r.scheduled_at)]
    lags = sorted(max(0, r.started_at - r.scheduled_at) * 1000 for r in cohort)
    active = sum(r.started_at < window.start <= r.completed_at for r in rows)
    peak = active
    events = sorted([(r.started_at, 1) for r in rows if window.contains(r.started_at)] +
                    [(r.completed_at, -1) for r in rows if window.contains(r.completed_at)])
    for _, delta in events:
        active += delta
        peak = max(peak, active)
    common = describe_metrics(
        rows, window, configured_offered_rps=offered_rps,
        slo_by_workload={name: (slo.ttft_p95_ms, slo.latency_p95_ms, slo.tpot_p95_ms)
                         for name in ({r.tags.get("workload") for r in rows} or {None})})
    return {
        "metrics": common,
        "start": window.start, "end": window.end, "duration_s": window.duration_s,
        "offered_rps": offered_rps, "n": m.n,
        "scheduled_rps": len(cohort) / window.duration_s,
        "attempted_rps": sum(window.contains(r.started_at) for r in rows) / window.duration_s,
        "successful_rps": m.request_throughput_rps,
        "throttled_rps": sum(r.throttled and window.contains(r.completed_at) for r in rows) / window.duration_s,
        "success_rate": m.success_rate, "throttle_rate": m.throttle_rate,
        "n_throttled": m.n_throttled, "slo_goodput_rps": m.slo_goodput_rps,
        "ttft_p95_ms": m.ttft_p95_ms, "latency_p95_ms": m.latency_p95_ms,
        "peak_inflight": peak,
        "scheduling_lag_p99_ms": lags[math.ceil(.99 * len(lags)) - 1] if lags else None,
    }


async def run_history_comparison(spec, target, subject, all_results, recover, on_progress=None):
    h = spec.history_protocol
    ceiling = spec.provider_ceilings[subject.name].rps
    if not ceiling:
        raise ValueError("history comparison requires a known nominal provider ceiling")
    slo = spec.slo_for(subject.name)
    arms = []
    for trial in range(spec.repetitions):
        # Reverse order on alternate trials to expose, rather than hide, order effects.
        scenarios = ["after_idle", "after_overload_recovery"]
        if trial % 2:
            scenarios.reverse()
        for index, rps in enumerate(spec.sweep_values(subject.name)):
            seed = None if spec.seed is None else spec.seed + trial * 10000 + index
            for scenario in scenarios:
                arm = {"scenario": scenario, "trial": trial, "target_rps": rps,
                       "nominal_ceiling_rps": ceiling, "seed": seed,
                       "status": "preparing", "bins": []}
                arms.append(arm)
                # Same idle preparation for both arms; a healthy probe is an
                # observed baseline, not a claim that the provider has reset.
                if not await recover(h.idle_s, f"history {scenario} rate {rps} trial {trial}: baseline"):
                    arm["status"] = "baseline_unhealthy"
                    return arms

                async def load(rate, seconds, phase):
                    runner = RateRunner(target, subject, rps=rate, duration_s=seconds,
                                        warmup_s=0, stream=spec.stream, seed=seed)
                    target.reset_peak()
                    rows = await runner.run()
                    for r in rows:
                        r.tags.update({"subject": subject.name, "sweep_type": "rate", "sweep_value": rate,
                                       "phase": phase, "scenario": scenario, "trial": trial,
                                       "window_start": runner.window.start, "window_end": runner.window.end,
                                       "measured": phase == "history_measurement" and runner.window.contains(r.scheduled_at)})
                    all_results.extend(rows)
                    return rows, runner.window

                if scenario == "after_overload_recovery":
                    rows, window = await load(ceiling * h.overload_quota_fraction,
                                              h.overload_duration_s, "history_overload")
                    arm["overload"] = describe_window(rows, window, slo, ceiling * h.overload_quota_fraction)
                    arm["overload"]["throttling_observed"] = any(r.throttled for r in rows)
                    # Rate > nominal ceiling is an offered overload, not proof
                    # that a hidden provider resource was exhausted.
                    if not await recover(h.recovery_s, f"history {scenario} rate {rps} trial {trial}: recovery"):
                        arm["status"] = "recovery_unhealthy"
                        return arms
                rows, window = await load(rps, spec.duration_s, "history_measurement")
                if "overload" in arm:
                    arm["seconds_since_overload_end"] = window.start - arm["overload"]["end"]
                arm["aggregate"] = describe_window(rows, window, slo, rps)
                for i in range(math.ceil(window.duration_s / h.bin_s)):
                    start = window.start + i * h.bin_s
                    b = MeasurementWindow(start, min(start + h.bin_s, window.end))
                    arm["bins"].append({"offset_s": i * h.bin_s, **describe_window(rows, b, slo, rps)})
                arm["status"] = "observed"
                if on_progress:
                    metrics = compute_run_metrics(rows, windows=[window], offered_rps=rps,
                                                  ttft_slo_ms=slo.ttft_p95_ms, tpot_slo_ms=slo.tpot_p95_ms,
                                                  latency_slo_ms=slo.latency_p95_ms)
                    on_progress(subject.name, rps, SweepPoint(concurrency=None, rps=rps, metrics=metrics,
                                                             phase=scenario))
    return arms
