import pytest

from bedrock_benchmark.analysis.metrics import MeasurementWindow
from bedrock_benchmark.analysis.observability import describe_metrics, describe_series
from bedrock_benchmark.results import RequestResult


def row(name, scheduled, start, end, **kw):
    return RequestResult(name, scheduled, start, end, tags={"workload": "w"}, **kw)


def test_window_cohort_drain_completions_usage_and_slo_goodput():
    rows = [
        row("warmup", -1, -1, 1, input_tokens=10, output_tokens=2, latency_ms=2000, ttft_ms=100),
        row("drain", 8, 8, 12, input_tokens=20, output_tokens=3, latency_ms=4000, ttft_ms=200),
        row("late", 9, 11, 12, success=False, throttled=True),
    ]
    m = describe_metrics(rows, MeasurementWindow(0, 10), slo_by_workload={"w": (500, 3000, None)})
    assert m["reliability"]["n"] == 2
    assert m["reliability"]["success_rate"] == .5
    assert m["throughput"]["scheduled_rps"] == .2
    assert m["throughput"]["attempted_rps"] == .1
    assert m["throughput"]["successful_rps"] == .1
    assert m["throughput"]["slo_goodput_rps"] == .1
    assert m["throughput"]["total_tokens_per_s"] == 1.2
    assert m["latency"]["e2e_ms"] == {"n": 1, "p50": 4000, "p95": 4000, "p99": 4000}
    assert m["load_state"]["outstanding_including_executor_queue"]["average"] is None
    assert m["load_state"]["outstanding_sdk_calls"]["average"] == .3


def test_time_weighted_queue_inclusive_outstanding_clips_boundaries():
    rows = [
        row("a", -2, -1, 3, submitted_at=-2),
        row("b", 1, 2, 5, submitted_at=1),
        row("c", 4, 6, 12, submitted_at=4),
    ]
    m = describe_metrics(rows, MeasurementWindow(0, 10))
    assert m["load_state"]["outstanding_including_executor_queue"] == {"peak": 2, "average": 1.3}
    assert m["load_state"]["outstanding_sdk_calls"] == {"peak": 2, "average": 1.0}


def test_missing_latency_usage_and_no_arrivals_are_explicit():
    r = row("missing", 1, 1, 2, output_tokens=4)
    m = describe_metrics([r], MeasurementWindow(0, 10))
    assert m["latency"]["e2e_ms"] == {"n": 0, "p50": None, "p95": None, "p99": None}
    assert m["throughput"]["input_tokens_per_s"] is None
    assert m["throughput"]["total_tokens_per_s"] is None
    assert m["throughput"]["output_tokens_per_s"] == .4
    # A bin with no arrivals can still have completed work.
    m = describe_metrics([r], MeasurementWindow(2, 3))
    assert m["reliability"]["success_rate"] is None
    assert m["throughput"]["successful_rps"] == 1
    assert m["throughput"]["output_tokens_per_s"] == 4
    assert describe_metrics([], MeasurementWindow(0, 1))["throughput"]["successful_rps"] == 0


def test_bins_show_late_failures_and_keep_existing_tpot_definition():
    clean = row("clean", 1, 1, 2, submitted_at=1, stream=True, output_tokens=3,
                latency_ms=1000, ttft_ms=100, last_text_latency_ms=500)
    bad = row("bad", 31, 31, 33, submitted_at=31, stream=True, success=False,
              stream_failure_stage="after_first_text", timed_out=True)
    series = describe_series([clean, bad], MeasurementWindow(0, 65))
    first, second, empty = series["bins"]
    assert [b["duration_s"] for b in series["bins"]] == [30, 30, 5]
    assert first["metrics"]["latency"]["tpot_ms"]["p50"] == 450
    assert first["metrics"]["latency"]["text_decode_tpot_ms"]["p50"] == 200
    assert second["metrics"]["reliability"]["non_throttle_error_rate"] == 1
    assert second["metrics"]["reliability"]["stream_failures_after_first_text"] == 1
    assert second["metrics"]["reliability"]["timeout_rate"] == 1
    assert empty["metrics"]["reliability"]["success_rate"] is None
    with pytest.raises(ValueError):
        describe_series([], MeasurementWindow(0, 1), bin_s=0)


def test_report_windows_include_per_class_metrics_and_empty_windows():
    from bedrock_benchmark.experiments.schema import load_experiment
    from bedrock_benchmark.models import ModelConfig
    from bedrock_benchmark.report import _measurement_windows
    spec = load_experiment("experiments/capacity-mix-rate.yaml",
                           ModelConfig(name="test", model_id="m", quota_rpm=400, quota_tpm=8000000))
    subject = spec.mix.name
    r = row("a", 1, 1, 2, latency_ms=1000, ttft_ms=100, output_tokens=10)
    r.tags.update(subject=subject, workload=spec.workloads[0].name, phase="confirmation",
                  sweep_value=1, repetition=0, window_start=0, window_end=60)
    windows = _measurement_windows(subject, spec, [r], [
        dict(subject=subject, phase="confirmation", value=1, repetition=1, start=100, end=160)])
    assert len(windows) == 2
    assert windows[0]["metrics"]["load_state"]["configured_offered_rps"] == 1
    assert len(windows[0]["classes"]) == len(spec.workloads)
    assert windows[0]["classes"][spec.workloads[0].name]["metrics"]["reliability"]["n"] == 1
    assert windows[1]["metrics"]["reliability"]["n"] == 0
