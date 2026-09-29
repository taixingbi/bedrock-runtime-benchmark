"""Descriptive metrics shared by all experiments; no additional SLO decisions.

Scheduled cohorts retain drain outcomes. Throughput counts completions in the
window. Bins never restart load and are never treated as independent tests.
"""
from __future__ import annotations

from .metrics import MeasurementWindow, percentile, tpot_ms


def distribution(values):
    values = [v for v in values if v is not None]
    return {"n": len(values), **{
        f"p{q}": round(percentile(values, q), 4) if values else None for q in (50, 95, 99)
    }}


def _outstanding(rows, window, submitted=False):
    # No submitted timestamp means queue-inclusive occupancy cannot be recovered.
    if submitted and any(r.submitted_at is None for r in rows):
        return {"peak": None, "average": None}
    events = []
    for r in rows:
        start = r.submitted_at if submitted else r.started_at
        start, end = max(start, window.start), min(r.completed_at, window.end)
        if end > start:
            events.extend(((start, 1), (end, -1)))
    active = peak = 0
    area = 0.0
    previous = window.start
    for timestamp, delta in sorted(events):
        area += active * (timestamp - previous)
        active += delta
        peak = max(peak, active)
        previous = timestamp
    return {"peak": peak, "average": round(area / window.duration_s, 4)}


def describe_metrics(rows, window, *, slo_by_workload=None, configured_concurrency=None,
                     configured_offered_rps=None, max_inflight=None):
    if window.duration_s <= 0:
        raise ValueError("measurement window must have positive duration")
    cohort = [r for r in rows if window.contains(r.scheduled_at)]
    successful = [r for r in cohort if r.success]
    completed = [r for r in rows if r.success and window.contains(r.completed_at)]
    n, seconds = len(cohort), window.duration_s
    rate = lambda count: round(count / n, 6) if n else None
    per_second = lambda count: round(count / seconds, 4)
    slos = slo_by_workload or {}

    def good(r):
        limits = slos.get(r.tags.get("workload"))
        if limits is None:
            return False
        values = (r.ttft_ms, r.latency_ms, tpot_ms(r))
        return all(limit is None or (value is not None and value <= limit)
                   for value, limit in zip(values, limits))

    inputs = [r.input_tokens for r in completed if r.input_tokens is not None]
    outputs = [r.output_tokens for r in completed if r.output_tokens is not None]
    # Fail closed on missing usage; known totals are not the full throughput.
    input_tps = per_second(sum(inputs)) if len(inputs) == len(completed) else None
    output_tps = per_second(sum(outputs)) if len(outputs) == len(completed) else None
    text_tpot = [
        (r.last_text_latency_ms - r.ttft_ms) / (r.output_tokens - 1)
        for r in successful if r.last_text_latency_ms is not None and r.ttft_ms is not None
        and r.output_tokens is not None and r.output_tokens >= 2
    ]
    failures = [r for r in cohort if not r.success]
    streaming = [r for r in cohort if r.stream is True]
    return {
        "latency": {
            "ttft_ms": distribution(r.ttft_ms for r in successful),
            "tpot_ms": distribution(tpot_ms(r) for r in successful),
            "e2e_ms": distribution(r.latency_ms for r in successful),
            "text_decode_tpot_ms": distribution(text_tpot),
        },
        "throughput": {
            "scheduled_rps": per_second(n),
            "attempted_rps": per_second(sum(window.contains(r.started_at) for r in rows)),
            "successful_rps": per_second(len(completed)),
            "slo_goodput_rps": per_second(sum(good(r) for r in completed)) if slos else None,
            "input_tokens_per_s": input_tps, "output_tokens_per_s": output_tps,
            "total_tokens_per_s": per_second(sum(inputs) + sum(outputs))
            if input_tps is not None and output_tps is not None else None,
            "completed_successes": len(completed),
            "input_usage_samples": len(inputs), "output_usage_samples": len(outputs),
        },
        "reliability": {
            "n": n, "n_success": len(successful),
            "n_throttled": sum(r.throttled for r in cohort),
            "n_non_throttle_errors": sum(not r.throttled for r in failures),
            "success_rate": rate(len(successful)),
            "throttle_rate": rate(sum(r.throttled for r in cohort)),
            "non_throttle_error_rate": rate(sum(not r.throttled for r in failures)),
            "timeout_rate": rate(sum(r.timed_out for r in cohort)),
            "streaming_requests": len(streaming),
            "stream_failures_before_first_text": sum(
                r.stream_failure_stage == "before_first_text" for r in failures),
            "stream_failures_after_first_text": sum(
                r.stream_failure_stage == "after_first_text" for r in failures),
            "stream_failures_stage_unknown": sum(
                r.stream is True and r.stream_failure_stage is None for r in failures),
            "stream_mode_unknown": sum(r.stream is None for r in cohort),
        },
        "load_state": {
            "configured_concurrency": configured_concurrency,
            "configured_offered_rps": configured_offered_rps,
            "max_inflight": max_inflight,
            "outstanding_including_executor_queue": _outstanding(rows, window, submitted=True),
            "outstanding_sdk_calls": _outstanding(rows, window),
            "scheduling_lag_ms": distribution(max(0, r.started_at - r.scheduled_at) * 1000 for r in cohort),
        },
    }


def describe_series(rows, window, *, bin_s=30.0, **kwargs):
    if bin_s <= 0:
        raise ValueError("bin_s must be positive")
    bins = []
    start = window.start
    while start < window.end:
        end = min(start + bin_s, window.end)
        bins.append({"offset_s": round(start - window.start, 6), "duration_s": end - start,
                     "metrics": describe_metrics(rows, MeasurementWindow(start, end), **kwargs)})
        start = end
    return {"start": window.start, "end": window.end, "duration_s": window.duration_s,
            "metrics": describe_metrics(rows, window, **kwargs), "bins": bins}
