import pytest
from bedrock_benchmark import cli
from bedrock_benchmark.batch import plan
from bedrock_benchmark.experiments.schema import load_experiment
from bedrock_benchmark.models import ModelConfig
from bedrock_benchmark.pilot import pilot_workloads
from bedrock_benchmark.run_file import estimated_duration_s

MODEL = ModelConfig(name='test', model_id='m', quota_rpm=400, quota_tpm=8000000)
PATH = 'experiments/capacity-shape-concurrency.yaml'
RETEST = {'workload': 'medium_context', 'concurrency': 7, 'duration_s': 1800}


def test_retest_isolated_from_default_sweep_and_preserves_confirmation():
    spec = load_experiment(PATH, MODEL, retest=RETEST)
    baseline = load_experiment(PATH, MODEL)
    assert spec.subject_names == ['medium_context']
    assert spec.name == 'capacity-shape-concurrency'
    assert spec.mode == "sustain"
    assert spec.retest == RETEST
    assert spec.sweep.values == [7] and spec.sweep.refinement is None
    assert spec.sweep.stop_after_fails is None
    assert spec.duration_s == spec.confirmation.min_steady_state_duration_s == 1800
    assert spec.confirmation.candidates == 1
    assert spec.confirmation.max_duration_s == spec.confirmation.max_requests == 'auto'
    assert spec.transport.max_connections == 128
    assert baseline.duration_s == 90 and len(baseline.subject_names) == 5
    assert baseline.sweep.refinement is not None and baseline.retest is None
    assert plan([PATH], [MODEL], retest=RETEST)[0].estimated_s == estimated_duration_s(spec)
    assert pilot_workloads([PATH], MODEL, retest=RETEST).subject_names == ['medium_context']


@pytest.mark.parametrize('changes', [{'workload': 'unknown'}, {'concurrency': 0}, {'concurrency': 1.5},
                                     {'duration_s': 0}, {'duration_s': float('inf')}])
def test_invalid_retest_parameters_fail_before_execution(changes):
    with pytest.raises(ValueError):
        load_experiment(PATH, MODEL, retest={**RETEST, **changes})


def test_reference_and_history_experiments_cannot_be_retested_as_calibration():
    for path in ('experiments/capacity-reference-concurrency.yaml', 'tests/fixtures/context-history-legacy.yaml'):
        with pytest.raises(ValueError, match='isolated admission_calibration'):
            load_experiment(path, MODEL, retest=RETEST)


def test_cli_plan_uses_retest_parameters(monkeypatch, capsys):
    monkeypatch.setattr(cli, '_models', lambda args: [MODEL])
    assert cli.main(['plan', 'capacity-shape-concurrency', '--model', 'test',
                     '--workload', 'medium_context', '--candidate-concurrency', '7',
                     '--steady-state-duration-s', '1800']) == 0
    output = capsys.readouterr().out
    assert 'capacity-shape-concurrency' in output
    assert 'concurrency [7]' in output
    assert 'tiny_request' not in output


def test_partial_cli_retest_is_rejected():
    args = cli._parser().parse_args(['plan', 'capacity-shape-concurrency', '--workload', 'medium_context'])
    with pytest.raises(ValueError, match='together'):
        cli._retest(args)


def test_batch_run_persists_effective_retest_spec(monkeypatch, tmp_path):
    import importlib
    import yaml
    from bedrock_benchmark.batch import run_batch
    from bedrock_benchmark.experiments.executor import ExperimentReport
    engine = importlib.import_module('bedrock_benchmark.run_file')
    async def fake_run(spec, **kwargs):
        assert spec.subject_names == ['medium_context']
        assert spec.duration_s == 1800 and spec.sweep.values == [7]
        return ExperimentReport(spec=spec)
    monkeypatch.setattr(engine, 'run_experiment', fake_run)
    result = run_batch([PATH], [MODEL], results_dir=tmp_path, retest=RETEST)
    assert result.exit_code == 0
    artifact = yaml.safe_load(open(result.results[0].profile_path))
    assert artifact['experiment'] == 'capacity-shape-concurrency'
    assert artifact['mode'] == 'sustain'
    assert artifact['measurement']['retest'] == RETEST
    assert artifact['measurement']['window_s'] == 1800
    assert artifact['sweep']['values'] == [7]
