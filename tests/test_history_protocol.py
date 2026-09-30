import asyncio
from dataclasses import replace

from bedrock_benchmark.analysis.metrics import MeasurementWindow
from bedrock_benchmark.experiments import history
from bedrock_benchmark.experiments.schema import load_experiment, HistoryProtocol, RecoveryProbe
from bedrock_benchmark.models import ModelConfig
from bedrock_benchmark.results import RequestResult
from bedrock_benchmark.run_file import estimated_duration_s


MODEL = ModelConfig(name="test", model_id="m", quota_rpm=400, quota_tpm=8000000)


def legacy_spec():
    spec = load_experiment("experiments/diagnostic-context-history.yaml", MODEL)
    return replace(spec, history_protocol=HistoryProtocol(), recovery_probe=RecoveryProbe(),
                   sweep=replace(spec.sweep, values=[1.6667]))


def test_history_pairs_rate_and_seed_reverses_order_and_does_not_restart_bins(monkeypatch):
    spec = legacy_spec()
    runs, recoveries, raw = [], [], []

    class Target:
        def reset_peak(self): pass

    class Runner:
        def __init__(self, target, subject, **kw):
            self.kw = kw
            self.subject = subject
            start = len(runs) * 2000
            self.window = MeasurementWindow(start, start + kw['duration_s'])
            runs.append(kw)

        async def run(self):
            return [RequestResult(request_id=str(i), scheduled_at=self.window.start + i,
                                  started_at=self.window.start + i, completed_at=self.window.start + i + .1,
                                  ttft_ms=20, latency_ms=100, input_tokens=8192, output_tokens=64,
                                  tags={"workload": self.subject.name}) for i in range(90)]

    async def recover(seconds, reason):
        recoveries.append((seconds, reason))
        return True

    monkeypatch.setattr(history, "RateRunner", Runner)
    arms = asyncio.run(history.run_history_comparison(spec, Target(), spec.workloads[0], raw, recover))
    assert [a['scenario'] for a in arms] == ['after_idle', 'after_overload_recovery',
                                            'after_overload_recovery', 'after_idle']
    assert len(runs) == 6  # 4 continuous observations + 2 overloads, not 120 bin runners
    assert len(recoveries) == 6
    assert all(len(a['bins']) == 30 for a in arms)
    assert arms[0]['seed'] == arms[1]['seed']
    assert arms[2]['seed'] == arms[3]['seed'] != arms[0]['seed']
    assert len({a['target_rps'] for a in arms}) == 1
    assert {r.tags['phase'] for r in raw} == {'history_overload', 'history_measurement'}
    assert all(not r.tags['measured'] for r in raw if r.tags['phase'] == 'history_overload')
    assert sum(b['n'] for b in arms[0]['bins']) == arms[0]['aggregate']['n']
    assert estimated_duration_s(spec) == 10800
    for arm in arms:
        assert "load_state" in arm["aggregate"]["metrics"]
        assert "tpot_ms" in arm["bins"][0]["metrics"]["latency"]
        assert sum(b["metrics"]["reliability"]["n"] for b in arm["bins"]) == arm["aggregate"]["metrics"]["reliability"]["n"]


def test_unhealthy_baseline_does_not_send_observation_traffic():
    spec = legacy_spec()
    async def unhealthy(*args): return False
    arms = asyncio.run(history.run_history_comparison(spec, None, spec.workloads[0], [], unhealthy))
    assert arms[0]['status'] == 'baseline_unhealthy'
    assert 'aggregate' not in arms[0]


def test_bin_counts_separate_arrival_start_and_completion():
    spec = load_experiment("experiments/diagnostic-context-history.yaml", MODEL)
    rows = [RequestResult(request_id='late', scheduled_at=29, started_at=31, completed_at=32,
                          latency_ms=1000, throttled=True, success=False)]
    first = history.describe_window(rows, MeasurementWindow(0,30), spec.slo_for(spec.workloads[0].name), 1)
    second = history.describe_window(rows, MeasurementWindow(30,60), spec.slo_for(spec.workloads[0].name), 1)
    assert first['n_throttled'] == 1 and first['attempted_rps'] == 0
    assert second['n'] == 0 and second['attempted_rps'] == 1/30 and second['throttled_rps'] == 1/30


def test_history_executor_emits_descriptive_artifact_without_capacity(monkeypatch):
    import jsonschema
    from bedrock_benchmark.client import BedrockConverseTarget
    from bedrock_benchmark.experiments.executor import run_experiment
    from bedrock_benchmark.report import build_capacity_profile
    from bedrock_benchmark.contract import schema_for
    from bedrock_benchmark.summary import summarize_entry
    from .fakes import FakeBedrockRuntimeClient
    spec = load_experiment('experiments/diagnostic-context-history.yaml', MODEL)
    seen = []
    def checkpoint(subject, arm, rows):
        pass
    async def comparison(spec, target, subject, raw, recover, on_progress, on_history_arm=None):
        assert on_history_arm is checkpoint
        seen.append(subject.name)
        return [{'scenario': 'after_idle', 'trial': 0, 'target_rps': 1, 'status': 'observed', 'bins': []}]
    monkeypatch.setattr(history, 'run_history_comparison', comparison)
    target = BedrockConverseTarget(model_id='m', client=FakeBedrockRuntimeClient())
    report = asyncio.run(run_experiment(spec, target=target, on_history_arm=checkpoint))
    artifact = build_capacity_profile(report)
    _, schema = schema_for(artifact)
    jsonschema.validate(artifact, schema)
    assert seen == spec.subject_names
    assert artifact['measurement']['history_protocol']['bin_s'] == 30
    for name in seen:
        entry = artifact['workload_classes'][name]
        assert 'history_comparison' in entry
        assert 'calibration_point' not in entry and 'rate' not in entry
        assert entry['recommendation']['admission_envelope'] is None
        assert 'Descriptive' in '\n'.join(summarize_entry(name, entry, artifact))


def test_medium_retest_uses_continuous_long_windows():
    spec = load_experiment('experiments/capacity-shape-concurrency.yaml', MODEL,
                           retest={'workload': 'medium_context', 'concurrency': 7, 'duration_s': 1800})
    assert spec.subject_names == ['medium_context']
    assert spec.sweep.values == [7]
    assert spec.duration_s == spec.confirmation.min_steady_state_duration_s == 1800
    assert spec.purpose == 'admission_calibration'
    assert spec.confirmation.max_requests == spec.confirmation.max_duration_s == 'auto'


def test_empty_history_window_has_zero_goodput_and_unknown_reliability():
    spec = load_experiment("experiments/diagnostic-context-history.yaml", MODEL)
    m = history.describe_window([], MeasurementWindow(0, 30),
                                spec.slo_for(spec.workloads[0].name), 1)["metrics"]
    assert m["throughput"]["slo_goodput_rps"] == 0
    assert m["reliability"]["success_rate"] is None


def test_fixed_wait_matrix_has_independent_overloads_and_no_probes(monkeypatch):
    spec = load_experiment("experiments/diagnostic-context-history.yaml", MODEL)
    spec = replace(spec, sweep=replace(spec.sweep, values=[0.1]))
    clock, events = [1000.0], []
    class Target:
        def reset_peak(self): pass
    class Runner:
        def __init__(self, target, subject, **kw):
            self.kw = kw
            self.window = MeasurementWindow(clock[0], clock[0] + kw['duration_s'])
        async def run(self):
            events.append(('load', self.kw['duration_s']))
            clock[0] = self.window.end + 2  # drain precedes recovery wait
            return [RequestResult(request_id='t', scheduled_at=self.window.start + 10,
                                  started_at=self.window.start + 10, completed_at=self.window.start + 10.1,
                                  throttled=True, success=False)]
    async def sleep(seconds):
        events.append(('wait', seconds))
        clock[0] += seconds
    async def forbidden(*args):
        raise AssertionError('fixed waits must not send recovery probes')
    monkeypatch.setattr(history, 'RateRunner', Runner)
    monkeypatch.setattr(history.asyncio, 'sleep', sleep)
    monkeypatch.setattr(history.time, 'time', lambda: clock[0])
    raw = []
    saved = []
    def checkpoint(subject, arm, rows):
        assert arm['status'] == 'observed'
        saved.append((subject, arm['requested_recovery_s'], list(rows)))
        events.append(('saved', arm['requested_recovery_s']))
    arms = asyncio.run(history.run_history_comparison(
        spec, Target(), spec.workloads[0], raw, forbidden, on_history_arm=checkpoint))
    assert len(saved) == 8
    assert [len(item[2]) for item in saved] == [1, 2, 2, 2, 2, 2, 2, 1]
    assert [r for item in saved for r in item[2]] == raw
    # Every arm is saved before the next independent baseline starts.
    for i, event in enumerate(events):
        if event[0] == 'saved' and i + 1 < len(events):
            assert events[i + 1] == ('wait', 300)
    assert [a['requested_recovery_s'] for a in arms] == [None, 120, 300, 600, 600, 300, 120, None]
    assert len([e for e in events if e == ('load', 120)]) == 6
    assert len([e for e in events if e == ('load', 900)]) == 8
    for a in arms:
        assert a['recovery_summary']['first_throttle_offset_s'] == 10
        assert a['recovery_summary']['initial_successful_rps'] == 0
        assert a['recovery_summary']['tail_start_offset_s'] == 600
        if a['requested_recovery_s'] is not None:
            assert a['seconds_since_overload_end'] == a['requested_recovery_s'] + 2
            assert a['seconds_since_overload_drain'] == a['requested_recovery_s']
    assert all('requested_recovery_s' in r.tags for r in raw)
    assert estimated_duration_s(spec) == 24720


def test_history_rejects_invalid_recovery_modes_and_delays():
    import pytest
    from bedrock_benchmark.experiments.schema import _validate
    spec = load_experiment('experiments/diagnostic-context-history.yaml', MODEL)
    for delays in ([], [0], [-1], [float('nan')], [float('inf')], [True], '120'):
        with pytest.raises(ValueError, match='recovery_delays_s'):
            _validate(replace(spec, history_protocol=replace(spec.history_protocol, recovery_delays_s=delays)))
    with pytest.raises(ValueError, match='recovery_mode'):
        _validate(replace(spec, history_protocol=replace(spec.history_protocol, recovery_mode='retry')))
