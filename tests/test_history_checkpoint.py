from dataclasses import replace
import importlib

import pytest
import yaml

from bedrock_benchmark.experiments.schema import load_experiment
from bedrock_benchmark.history_checkpoint import HistoryCheckpoints
from bedrock_benchmark.results import RequestResult
from bedrock_benchmark.storage import read_jsonl
from .test_history_protocol import MODEL


def spec():
    original = load_experiment('tests/fixtures/context-history-legacy.yaml', MODEL)
    return replace(original, workloads=original.workloads[:1], repetitions=1,
                   sweep=replace(original.sweep, values=[0.1]),
                   history_protocol=replace(original.history_protocol, recovery_delays_s=[120]))


def arm(scenario='after_idle', status='observed'):
    return {'scenario': scenario, 'trial': 0, 'target_rps': 0.1, 'status': status,
            'bins': [], 'requested_recovery_s': None if scenario == 'after_idle' else 120}


def test_completed_and_unhealthy_arms_remain_readable(tmp_path):
    writer = HistoryCheckpoints(tmp_path / 'checkpoints', spec(), {'run_id': 'test'})
    rows = [RequestResult(request_id='saved', scheduled_at=0, started_at=0, completed_at=1, success=True)]
    writer.save_arm('long_context_short_answer', arm(), rows)
    writer.save_arm('long_context_short_answer', arm('after_overload_recovery', 'recovery_unhealthy'), [])
    writer.finish()
    manifest = yaml.safe_load((writer.directory / 'manifest.yaml').read_text())
    assert manifest['status'] == 'incomplete'
    assert manifest['planned_arms'] == manifest['saved_arms'] == 2
    assert manifest['observed_arms'] == 1
    assert read_jsonl(str(writer.directory / 'arm-0001.jsonl'))[0].request_id == 'saved'
    assert yaml.safe_load((writer.directory / 'arm-0002.yaml').read_text())['status'] == 'recovery_unhealthy'


def test_manifest_does_not_publish_half_written_arm(tmp_path, monkeypatch):
    module = importlib.import_module('bedrock_benchmark.history_checkpoint')
    writer = HistoryCheckpoints(tmp_path / 'checkpoints', spec(), {})
    writer.save_arm('long_context_short_answer', arm(), [])
    original = module._write_yaml
    def fail_summary(path, value):
        if path.name == 'arm-0002.yaml':
            raise OSError('disk full')
        return original(path, value)
    monkeypatch.setattr(module, '_write_yaml', fail_summary)
    with pytest.raises(OSError, match='disk full'):
        writer.save_arm('long_context_short_answer', arm('after_overload_recovery'), [])
    manifest = yaml.safe_load((writer.directory / 'manifest.yaml').read_text())
    assert manifest['saved_arms'] == 1
    assert len(manifest['arms']) == 1
    assert (writer.directory / manifest['arms'][0]['summary_file']).is_file()


@pytest.mark.parametrize('error,status', [(KeyboardInterrupt, 'interrupted'), (RuntimeError, 'failed')])
def test_run_file_preserves_arm_on_interruption(tmp_path, monkeypatch, error, status):
    module = importlib.import_module('bedrock_benchmark.run_file')
    async def interrupted(spec, *, on_progress, target, on_history_arm):
        on_history_arm(spec.subject_names[0], arm(), [RequestResult(request_id='before-stop', scheduled_at=0, started_at=0, completed_at=1)])
        raise error()
    monkeypatch.setattr(module, 'run_experiment', interrupted)
    with pytest.raises(error):
        module.run_file('tests/fixtures/context-history-legacy.yaml', MODEL, results_dir=str(tmp_path))
    manifest_path, = tmp_path.glob('test/*-checkpoints/manifest.yaml')
    manifest = yaml.safe_load(manifest_path.read_text())
    assert manifest['status'] == status
    assert manifest['saved_arms'] == 1
    assert manifest['planned_arms'] == 80
    assert read_jsonl(str(manifest_path.parent / 'arm-0001.jsonl'))[0].request_id == 'before-stop'
    assert not list(tmp_path.glob('test/*-capacity-profile.yaml'))


def test_complete_only_after_all_arms_observed(tmp_path):
    writer = HistoryCheckpoints(tmp_path / 'checkpoints', spec(), {})
    writer.save_arm('long_context_short_answer', arm(), [])
    writer.save_arm('long_context_short_answer', arm('after_overload_recovery'), [])
    writer.finish()
    assert yaml.safe_load((writer.directory / 'manifest.yaml').read_text())['status'] == 'complete'
