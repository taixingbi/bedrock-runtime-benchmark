"""Mix-scoped capacity: budgets, composition, and workflow selection."""
import random
from collections import Counter

import pytest
import yaml

from bedrock_benchmark.analysis.confirmation import candidate_caps, limits_for, plan_looks
from bedrock_benchmark.batch import plan
from bedrock_benchmark.cli import _parser
from bedrock_benchmark.experiments.schema import load_experiment
from bedrock_benchmark.models import ModelConfig
from bedrock_benchmark.workload import WorkloadMix, WorkloadProfile, block_counts


MODEL = ModelConfig(name="slow", model_id="m", quota_rpm=10, quota_tpm=10000000)


def test_low_rpm_confirmation_gets_enough_time():
    spec = load_experiment("experiments/mixed-capacity.yaml", MODEL)
    shares = spec.mix.weights
    limits = limits_for({}, {w.name: {"throttle_rate_max": spec.slo_for(w.name).throttle_rate_max,
                                     "success_rate_min": spec.slo_for(w.name).success_rate_min}
                             for w in spec.workloads}, shares)
    schedule = plan_looks(limits, confidence=.95, max_looks=2, max_repetitions=None,
                          max_requests="auto", max_duration_s="auto")
    slow = candidate_caps(schedule, "auto", "auto", est_requests_per_rep=15, per_rep_s=100)
    fast = candidate_caps(schedule, "auto", "auto", est_requests_per_rep=150, per_rep_s=100)
    assert slow["max_duration_s"] > 1800
    assert slow["max_duration_s"] >= schedule.look_schedule[-1] / 15 * 100
    assert slow["max_duration_s"] > fast["max_duration_s"]
    assert slow["max_requests"] >= schedule.look_schedule[-1]
    fixed = candidate_caps(schedule, 10000, 1800, est_requests_per_rep=15, per_rep_s=100)
    assert fixed == {"max_requests": 10000, "max_duration_s": 1800.0, "per_candidate": False}


def test_stratified_blocks_and_seed_reproducibility():
    entries = [(WorkloadProfile(name=n, input_tokens=10, output_tokens=10), w)
               for n, w in {"chat": .6, "rag": .3, "gen": .1}.items()]
    mix = WorkloadMix(name="test", entries=entries, assignment="stratified")
    def draw(seed):
        rng = random.Random(seed)
        return [mix.sample(rng).name for _ in range(100)]
    draws = draw(42)
    assert draws == draw(42)
    for i in range(0, 100, 10):
        assert Counter(draws[i:i+10]) == {"chat": 6, "rag": 3, "gen": 1}
    assert block_counts({"common": .999, "rare": .001}) == {"common": 999, "rare": 1}
    with pytest.raises(ValueError, match="stochastic"):
        block_counts({"common": .99999, "rare": .00001})


def test_workflow_catalog_override(tmp_path, monkeypatch):
    catalog = tmp_path / "mixes.yaml"
    catalog.write_text(yaml.safe_dump({"mixes": {"support": {
        "source": "production_traffic_profile", "observed_from": "workflow logs, September",
        "weights": {"rag_answer": .8, "short_chat": .2},
    }}}))
    spec = load_experiment("experiments/mixed-capacity.yaml", MODEL, mix="support", mixes_file=str(catalog))
    assert spec.mix.name == "support"
    assert spec.mix.source == "production_traffic_profile"
    assert {w.name for w in spec.workloads} == {"rag_answer", "short_chat"}
    assert spec.transport.max_connections == 128
    assert spec.subject_names == ["support"]
    args = _parser().parse_args(["plan", "mixed-capacity", "--model", "slow", "--mix", "support"])
    assert args.mix == "support"
    # Batch planning must forward the CLI override to the loader.
    import bedrock_benchmark.batch as batch
    original = batch.load_experiment
    monkeypatch.setattr(batch, "load_experiment", lambda *a, **kw: original(*a, **kw, mixes_file=str(catalog)))
    assert plan(["experiments/mixed-capacity.yaml"], [MODEL], mix="support")[0].estimated_s > 1800
