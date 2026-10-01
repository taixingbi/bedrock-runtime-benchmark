"""The second experiment must use confirmed, matching baseline evidence."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from bedrock_benchmark.context_history import binding, prepare
from bedrock_benchmark.experiments.schema import load_experiment, NoMatchingWorkloads
from bedrock_benchmark.run_file import estimated_duration_s
from .test_actions_matrix import build_matrix
from .test_history_protocol import MODEL

TEMPLATE = Path('experiments/diagnostic-context-history.yaml')
RATES = {'short': 3, 'medium': 1.5, 'long': 0.8}


@pytest.fixture
def profiles(tmp_path):
    paths = []
    for name, rate in RATES.items():
        spec = load_experiment(f'experiments/capacity-context-{name}-rate.yaml', MODEL)
        b = binding(spec)
        artifact = {
            'experiment': spec.name, 'purpose': spec.purpose, 'mode': 'sweep',
            'model': b['model'],
            'constraints': {'quota': b['quota'], 'workloads': b['workloads'],
                            'slo': {'effective_by_workload': b['slo']}},
            'workload_classes': {name: {
                'calibration_point': {'statistically_confirmed_rate': rate},
                'workload_validation': {'valid': True, 'input': {'valid': True}, 'output': {'valid': True}},
            }},
        }
        path = tmp_path / f'{name}.yaml'
        path.write_text(yaml.safe_dump(artifact))
        paths.append(path)
    return paths


def test_main_template_cannot_send_unbound_rates():
    with pytest.raises(NoMatchingWorkloads, match='Experiment A'):
        load_experiment(str(TEMPLATE), MODEL)
    with pytest.raises(ValueError, match='Experiment B'):
        build_matrix(TEMPLATE.stem)


def test_baseline_shapes_and_independent_grids():
    for name, inputs, grid in [('short', 512, [.5, 1, 2, 3, 4]),
                               ('medium', 2048, [.5, 1, 1.5, 2]),
                               ('long', 8192, [.25, .5, .75, 1])]:
        spec = load_experiment(f'experiments/capacity-context-{name}-rate.yaml', MODEL)
        assert [(w.name, w.input_tokens, w.output_tokens) for w in spec.workloads] == [(name, inputs, 64)]
        assert spec.sweep_values(name) == grid
        assert spec.confirmation.cooldown_s == 300
        assert spec.confirmation.continuous
        assert spec.confirmation.min_steady_state_duration_s == 900


def test_generate_load_and_split_all_nine_cells(profiles, tmp_path):
    configs = prepare(TEMPLATE, profiles, MODEL)
    original = yaml.safe_load(TEMPLATE.read_text())
    experiments = tmp_path / 'experiments'
    experiments.mkdir()
    total, jobs = 0, []
    for cfg in configs:
        name = cfg['workloads'][0]
        assert cfg['sweep']['values'] == pytest.approx([RATES[name] * f for f in [.5, .75, .9]])
        for key in ('history_protocol', 'repetitions', 'duration_s', 'transport'):
            assert cfg[key] == original[key]
        path = experiments / f'{cfg["name"]}.yaml'
        path.write_text(yaml.safe_dump(cfg))
        spec = load_experiment(str(path), MODEL)
        total += estimated_duration_s(spec)
        cells = build_matrix(cfg['name'], tmp_path)['include']
        for i, cell in enumerate(cells):
            cell_path = tmp_path / f'{cell["id"]}.yaml'
            cell_path.write_text(yaml.safe_dump(cell['config']))
            cell_spec = load_experiment(str(cell_path), MODEL)
            assert cell_spec.seed == spec.seed + i
            assert cell_spec.baseline_context['load_fractions'] == [[.5], [.75], [.9]][i]
        jobs.extend(cells)
        with pytest.raises(ValueError, match='baseline model'):
            load_experiment(str(path), replace(MODEL, model_id='different'))
        changed = deepcopy(cfg)
        changed['sweep']['values'][0] += .1
        path.write_text(yaml.safe_dump(changed))
        with pytest.raises(ValueError, match='rates differ'):
            load_experiment(str(path), MODEL)
    assert len(jobs) == 9
    assert total == 111240


@pytest.mark.parametrize('case', ['unconfirmed', 'output_invalid', 'input_unknown', 'quota', 'slo', 'concurrency'])
def test_reject_unusable_baselines(profiles, case):
    path = profiles[0]
    profile = yaml.safe_load(path.read_text())
    entry = profile['workload_classes']['short']
    if case == 'unconfirmed':
        entry['calibration_point']['statistically_confirmed_rate'] = None
    elif case == 'output_invalid':
        entry['workload_validation']['output']['valid'] = False
    elif case == 'input_unknown':
        entry['workload_validation']['input']['valid'] = None
    elif case == 'quota':
        profile['constraints']['quota']['rpm'] += 1
    elif case == 'slo':
        profile['constraints']['slo']['effective_by_workload']['short']['ttft_p95_ms'] += 1
    else:
        entry['calibration_point'] = {'statistically_confirmed_concurrency': 3}
    path.write_text(yaml.safe_dump(profile))
    with pytest.raises(ValueError):
        prepare(TEMPLATE, profiles, MODEL)


def test_missing_and_duplicate_baselines(profiles):
    with pytest.raises(ValueError, match='Missing'):
        prepare(TEMPLATE, profiles[:-1], MODEL)
    with pytest.raises(ValueError, match='Duplicate'):
        prepare(TEMPLATE, profiles + profiles[:1], MODEL)


def test_normalized_history_records_baseline_in_arms_requests_and_checkpoints(profiles, tmp_path, monkeypatch):
    import asyncio
    from bedrock_benchmark.analysis.metrics import MeasurementWindow
    from bedrock_benchmark.experiments import history
    from bedrock_benchmark.history_checkpoint import HistoryCheckpoints
    from bedrock_benchmark.results import RequestResult

    cfg = prepare(TEMPLATE, profiles, MODEL)[0]
    path = tmp_path / 'history.yaml'
    path.write_text(yaml.safe_dump(cfg))
    spec = load_experiment(str(path), MODEL)
    waits, runs, rows = [], [], []

    class Target:
        def reset_peak(self):
            pass

    class Runner:
        def __init__(self, target, subject, **kwargs):
            self.subject = subject
            self.window = MeasurementWindow(len(runs) * 2000, len(runs) * 2000 + kwargs['duration_s'])
            runs.append(kwargs)

        async def run(self):
            return [RequestResult(request_id=str(len(runs)), scheduled_at=self.window.start,
                                  started_at=self.window.start, completed_at=self.window.start + .1,
                                  success=True, ttft_ms=10, latency_ms=100,
                                  input_tokens=512, output_tokens=64,
                                  tags={'workload': self.subject.name})]

    async def sleep(seconds):
        waits.append(seconds)

    async def no_probes(*args):
        raise AssertionError('Fixed-wait history must not probe')

    monkeypatch.setattr(history, 'RateRunner', Runner)
    monkeypatch.setattr(history.asyncio, 'sleep', sleep)
    checkpoints = HistoryCheckpoints(tmp_path / 'checkpoints', spec, {})
    arms = asyncio.run(history.run_history_comparison(
        spec, Target(), spec.workloads[0], rows, no_probes, on_history_arm=checkpoints.save_arm))
    assert len(arms) == 24
    assert len(runs) == 42  # 24 observations + 18 overloads
    assert {a['load_fraction'] for a in arms} == {.5, .75, .9}
    assert all(a['target_rps'] == a['load_fraction'] * a['r_safe_idle_rps'] for a in arms)
    assert all(r.tags['r_safe_idle_rps'] == 3 for r in rows)
    assert checkpoints.manifest['baseline_context'] == cfg['baseline_context']
    assert checkpoints.manifest['saved_arms'] == 24
    assert waits.count(120) == waits.count(600) == 6
    assert all(run['warmup_s'] == 0 for run in runs)
