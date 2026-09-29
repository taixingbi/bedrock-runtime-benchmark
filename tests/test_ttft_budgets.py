import pytest
import yaml

from bedrock_benchmark.constraints import load_slo, ttft_budget_for
from bedrock_benchmark.experiments.schema import load_experiment
from bedrock_benchmark.experiments.executor import ExperimentReport
from bedrock_benchmark.models import ModelConfig
from bedrock_benchmark.report import build_capacity_profile

MODEL = ModelConfig(name="test", model_id="m", quota_rpm=400, quota_tpm=8000000)


@pytest.mark.parametrize("tokens,expected", [(1,800),(512,800),(513,1500),(4096,1500),(4097,3000),(16384,3000)])
def test_inclusive_input_band_boundaries(tokens, expected):
    _, b = ttft_budget_for(load_slo().ttft_budgets, tokens)
    assert b.ttft_p95_ms == expected


def test_same_input_budget_different_output_and_reliability():
    spec = load_experiment("experiments/capacity-reference-concurrency.yaml", MODEL)
    rag, generation = (spec.slo_for(n) for n in ("rag_answer", "long_generation"))
    assert rag.ttft_p95_ms == generation.ttft_p95_ms == 1500
    assert (rag.tpot_p95_ms, generation.tpot_p95_ms) == (70,120)
    assert (rag.throttle_rate_max, generation.throttle_rate_max) == (.005,.01)
    assert (rag.latency_p95_ms, generation.latency_p95_ms) == (10000,60000)
    spec = load_experiment("experiments/capacity-shape-concurrency.yaml", MODEL)
    assert spec.slo_for("long_context_short_answer").ttft_p95_ms == 3000
    assert spec.slo_for("long_context_short_answer").throttle_rate_max == .005
    profile = build_capacity_profile(ExperimentReport(spec=spec))
    policy = profile["constraints"]["slo"]
    assert policy["effective_by_workload"]["long_context_short_answer"]["ttft_budget"] == "long_input"
    assert policy["effective_by_workload"]["long_generation"]["ttft_p95_ms"] == 1500
    assert policy["profiles"]["bronze"]["ttft_p95_ms"] is None


@pytest.mark.parametrize("bands", [
    {}, {"a": {"max_input_tokens": 512, "ttft_p95_ms": 800}},
    {"a": {"max_input_tokens": None, "ttft_p95_ms": 800},
     "b": {"max_input_tokens": 512, "ttft_p95_ms": 1500}},
    {"a": {"max_input_tokens": 512, "ttft_p95_ms": 800},
     "b": {"max_input_tokens": 256, "ttft_p95_ms": 1500},
     "c": {"max_input_tokens": None, "ttft_p95_ms": 3000}},
    {"a": {"max_input_tokens": None, "ttft_p95_ms": float("nan")}},
    {"a": {"max_input_tokens": None, "ttft_p95_ms": 0}},
])
def test_invalid_bands_fail_before_measurement(tmp_path, bands):
    p = tmp_path / "slo.yaml"
    p.write_text(yaml.safe_dump({"profiles":{"p":{}}, "ttft_budgets":bands},sort_keys=False))
    with pytest.raises(ValueError):
        load_slo(str(p))


def test_legacy_profiles_work_but_conflicting_rules_rejected(tmp_path):
    p = tmp_path / "slo.yaml"
    raw = {"profiles":{"gold":{"ttft_p95_ms":777}, "silver":{}, "bronze":{}}}
    p.write_text(yaml.safe_dump(raw))
    spec = load_experiment("experiments/capacity-reference-concurrency.yaml", MODEL, slo_file=str(p))
    assert spec.slo_for("short_chat").ttft_p95_ms == 777
    raw["ttft_budgets"] = {"all":{"max_input_tokens":None, "ttft_p95_ms":1000}}
    p.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError, match="not both"):
        load_slo(str(p))


def test_input_budget_changes_gate_and_goodput_without_changing_reliability():
    from bedrock_benchmark.results import RequestResult
    from bedrock_benchmark.analysis.metrics import compute_run_metrics
    from bedrock_benchmark.analysis.capacity import SweepPoint, point_verdict
    from bedrock_benchmark.experiments.executor import _slo_kwargs
    spec = load_experiment("experiments/capacity-shape-concurrency.yaml", MODEL)
    rows = [RequestResult(str(i), i, i, i+3, ttft_ms=2500, latency_ms=3000, output_tokens=64)
            for i in range(1000)]
    for name, expected, goodput in [("medium_context", "FAIL", 0),
                                    ("long_context_short_answer", "PASS", 1)]:
        slo = spec.slo_for(name)
        m = compute_run_metrics(rows, duration_s=1000, ttft_slo_ms=slo.ttft_p95_ms,
                                tpot_slo_ms=slo.tpot_p95_ms, latency_slo_ms=slo.latency_p95_ms)
        verdict = point_verdict(SweepPoint(concurrency=1, rps=None, metrics=m), **_slo_kwargs(slo))
        assert verdict.verdict == expected
        assert m.slo_goodput_rps == goodput
        assert m.success_rate == 1 and m.throttle_rate == 0
