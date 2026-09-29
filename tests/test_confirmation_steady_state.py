"""Deterministic windows for burst-then-throttle confirmation regressions."""
import asyncio

import pytest

from bedrock_benchmark.analysis.metrics import MeasurementWindow
from bedrock_benchmark.client import BedrockConverseTarget
from bedrock_benchmark.experiments import executor
from bedrock_benchmark.experiments.schema import ConfirmationConfig, ExperimentSpec, MixConfig, SloConfig, SweepConfig, TargetConfig
from bedrock_benchmark.report import build_capacity_profile
from bedrock_benchmark.results import RequestResult
from bedrock_benchmark.workload import WorkloadProfile
from .fakes import FakeBedrockRuntimeClient


def run_windows(monkeypatch, windows, *, minimum=300, max_requests=8000, max_repetitions=None, values=None, mixed=False):
    """Each pair is (clean requests, throttles); first window is discovery."""
    remaining = iter(windows)
    calls = []

    class ScriptedRunner:
        def __init__(self, target, subject, *, duration_s, warmup_s, **kwargs):
            self.subject = subject
            start = 1000 + len(calls) * 1000 + warmup_s
            self.window = MeasurementWindow(start, start + duration_s)
            calls.append(self)

        async def run(self):
            clean, bad = next(remaining)
            rows = []
            for i in range(clean + bad):
                t = self.window.start + (i + .5) / (clean + bad) * (self.window.end - self.window.start)
                rows.append(RequestResult(
                    request_id=f"{len(calls)}-{i}", scheduled_at=t, started_at=t, completed_at=t + .1,
                    latency_ms=100, input_tokens=2048, output_tokens=128, success=i < clean,
                    throttled=i >= clean, error_code=None if i < clean else "ThrottlingException",
                    tags={"workload": ("rare" if i >= clean or i % 10 == 0 else "medium_context")
                          if mixed else self.subject.name},
                ))
            return rows

    monkeypatch.setattr(executor, "ConcurrencyRunner", ScriptedRunner)
    spec = ExperimentSpec(
        name="steady", purpose="reference" if mixed else "admission_calibration", target=TargetConfig(model_id="m"),
        workloads=[WorkloadProfile(name="medium_context", input_tokens=2048, output_tokens=128)] + (
            [WorkloadProfile(name="rare", input_tokens=2048, output_tokens=128)] if mixed else []),
        mix=MixConfig(name="blend", weights={"medium_context": .9, "rare": .1}) if mixed else None,
        sweep=SweepConfig(type="concurrency", values=values or [8]),
        # 97.5% family confidence / 2 looks -> exactly N=875 at 0.5% throttle.
        slo=SloConfig(throttle_rate_max=.005, success_rate_min=.99, latency_p95_ms=3000, confidence=.975),
        duration_s=90, warmup_s=100, repetitions=1, stream=False,
        confirmation=ConfirmationConfig(min_steady_state_duration_s=minimum, max_requests=max_requests,
                                        max_duration_s="auto", max_repetitions=max_repetitions, candidates=len(values or [8])),
    )
    target = BedrockConverseTarget(model_id="m", client=FakeBedrockRuntimeClient())
    report = asyncio.run(executor.run_experiment(spec, target=target))
    artifact = build_capacity_profile(report)
    return report, artifact["mixed_workloads"]["blend"] if mixed else artifact["workload_classes"]["medium_context"]


def test_original_875_clean_then_713_throttles_never_confirms(monkeypatch):
    report, artifact = run_windows(monkeypatch, [(900, 0), (800, 0), (649, 713)], minimum=0)
    [candidate] = report.profiles[0].confirmations
    assert candidate.decision_n == 875
    assert candidate.decision_metrics.n_throttled == 0
    assert candidate.n == 2162
    assert candidate.point.metrics.n_throttled == 713
    assert (candidate.verdict, candidate.stop_reason) == ("INCONCLUSIVE", "post_look_violation")
    assert artifact["concurrency"]["statistically_confirmed"] is None
    assert artifact["calibration_point"]["statistically_confirmed_concurrency"] is None
    assert artifact["measurement_validity"]["status"] == "suspect_steady_state"
    assert {v["scope"] for v in candidate.steady_state["violations"]} == {
        "all_collected", "latest_window", "post_look"}


def test_clean_pass_waits_for_measured_time_and_preserves_fixed_look(monkeypatch):
    report, artifact = run_windows(monkeypatch, [(900, 0)] * 5, max_requests="auto")
    [candidate] = report.profiles[0].confirmations
    assert candidate.verdict == "PASS"
    assert candidate.repetitions == 4
    assert candidate.decision_n == 875
    assert candidate.looks_used == 1
    assert candidate.steady_state["measured_duration_s"] == 360
    assert candidate.steady_state["minimum_duration_met"]
    assert artifact["calibration_point"]["statistically_confirmed_concurrency"] == 8
    assert candidate.caps["max_requests"] >= 3600


def test_later_window_revokes_pending_pass(monkeypatch):
    report, artifact = run_windows(monkeypatch, [(900, 0), (900, 0), (0, 900)])
    [candidate] = report.profiles[0].confirmations
    assert candidate.decision_n == 875
    assert candidate.decision_metrics.n_throttled == 0
    assert candidate.stop_reason == "post_look_violation"
    assert candidate.steady_state["statistical_look_passed"]
    assert artifact["calibration_point"]["statistically_confirmed_concurrency"] is None


@pytest.mark.parametrize("limits", [{"max_requests": 900}, {"max_repetitions": 1}])
def test_caps_cannot_turn_short_clean_prefix_into_confirmation(monkeypatch, limits):
    report, artifact = run_windows(monkeypatch, [(900, 0)] * 2, **limits)
    [candidate] = report.profiles[0].confirmations
    assert candidate.verdict == "INCONCLUSIVE"
    assert candidate.steady_state["statistical_look_passed"]
    assert not candidate.steady_state["minimum_duration_met"]
    assert artifact["calibration_point"]["statistically_confirmed_concurrency"] is None


def test_empty_post_look_is_not_a_violation(monkeypatch):
    report, _ = run_windows(monkeypatch, [(900, 0), (875, 0)], minimum=0)
    [candidate] = report.profiles[0].confirmations
    assert candidate.verdict == "PASS"
    assert candidate.steady_state["violations"] == []


def test_latest_bad_window_cannot_hide_in_a_clean_aggregate(monkeypatch):
    report, _ = run_windows(monkeypatch, [(30000, 0), (30000, 0), (0, 100)], max_requests=100000)
    [candidate] = report.profiles[0].confirmations
    assert candidate.point.metrics.throttle_rate < .005
    assert candidate.stop_reason == "post_look_violation"
    assert "latest_window" in {v["scope"] for v in candidate.steady_state["violations"]}


def test_rejected_high_candidate_allows_clean_lower_candidate(monkeypatch):
    report, artifact = run_windows(monkeypatch, [(1500, 0), (1500, 0), (1500, 713)] + [(1500, 0)] * 4,
                                   values=[6, 8])
    high, low = report.profiles[0].confirmations
    assert (high.value, high.stop_reason) == (8, "post_look_violation")
    assert (low.value, low.verdict) == (6, "PASS")
    assert artifact["calibration_point"]["statistically_confirmed_concurrency"] == 6
    assert low.steady_state["measured_duration_s"] == 360


def test_new_validity_status_conforms_to_artifact_contract(monkeypatch):
    import jsonschema
    from bedrock_benchmark.contract import schema_for
    report, _ = run_windows(monkeypatch, [(900, 0), (800, 0), (649, 713)], minimum=0)
    artifact = build_capacity_profile(report)
    _, schema = schema_for(artifact)
    jsonschema.validate(artifact, schema)


@pytest.mark.parametrize("minimum", [-1, float("nan"), float("inf"), "auto", None])
def test_minimum_duration_is_a_finite_nonnegative_number(minimum):
    from dataclasses import replace
    from bedrock_benchmark.experiments.schema import _validate, load_experiment
    from bedrock_benchmark.models import ModelConfig
    spec = load_experiment("experiments/capacity-shape-concurrency.yaml", ModelConfig(name="m", model_id="m"))
    spec.confirmation = replace(spec.confirmation, min_steady_state_duration_s=minimum)
    with pytest.raises(ValueError, match="min_steady_state_duration_s"):
        _validate(spec)


def test_mix_checks_each_class_even_when_blend_hides_throttles(monkeypatch):
    report, artifact = run_windows(monkeypatch, [(9000, 0), (9000, 0), (9000, 100)],
                                   max_requests=100000, mixed=True)
    [candidate] = report.profiles[0].confirmations
    assert candidate.stop_reason == "post_look_violation"
    assert candidate.point.metrics.throttle_rate < .01
    assert any("rare.throttle_rate" in v["failed_checks"] for v in candidate.steady_state["violations"])
    assert artifact["recommendation"]["admission_envelope"] is None


def test_non_monotonic_discovery_is_flagged(monkeypatch):
    report, artifact = run_windows(monkeypatch, [(900, 0), (0, 900), (900, 0)] + [(1500, 0)] * 4,
                                   values=[1, 2, 4])
    assert report.profiles[0].analysis.status == "unresolved"
    assert artifact["measurement_validity"]["status"] == "suspect_non_monotonic"
    assert any(e["outcome"] == "non_monotonic" for e in artifact["measurement_validity"]["events"])
