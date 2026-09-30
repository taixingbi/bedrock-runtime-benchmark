"""Local, per-arm history artifacts. These are descriptive, not capacity profiles."""
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .storage import write_jsonl


def _write_yaml(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(yaml.safe_dump(value, sort_keys=False), encoding='utf-8')
    temporary.replace(path)


class HistoryCheckpoints:
    def __init__(self, directory, spec, metadata):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=False)
        h = spec.history_protocol
        total = sum(len(spec.sweep_values(n)) for n in spec.subject_names) * spec.repetitions * (
            1 + len(h.recovery_delays_s or [h.recovery_s]))
        self.manifest = {
            'schema_version': 1, 'artifact_type': 'history_checkpoints',
            'experiment': spec.name, 'run': metadata, 'target': asdict(spec.target),
            'status': 'running', 'planned_arms': total, 'saved_arms': 0, 'observed_arms': 0,
            'protocol': asdict(h), 'duration_s': spec.duration_s,
            'repetitions': spec.repetitions, 'seed': spec.seed, 'stream': spec.stream,
            'configured_workloads': [asdict(w) for w in spec.workloads],
            'workloads': {n: {'rates_rps': spec.sweep_values(n), 'slo': asdict(spec.slo_for(n))}
                          for n in spec.subject_names},
            'interpretation': 'descriptive; does not establish SLO compliance or provider reset',
            'arms': [],
        }
        self._save_manifest()

    def _save_manifest(self):
        self.manifest['updated_at'] = datetime.now(timezone.utc).isoformat()
        _write_yaml(self.directory / 'manifest.yaml', self.manifest)

    def save_arm(self, subject, arm, rows):
        stem = f"arm-{len(self.manifest['arms']) + 1:04d}"
        raw = self.directory / f'{stem}.jsonl'
        temporary = raw.with_suffix('.jsonl.tmp')
        write_jsonl(rows, str(temporary))
        temporary.replace(raw)
        summary = self.directory / f'{stem}.yaml'
        _write_yaml(summary, {'artifact_type': 'history_arm', 'subject': subject,
                              'run': self.manifest['run'], 'raw_file': raw.name, **arm})
        # Publish the entry only after both artifacts are fully written.
        self.manifest['arms'].append({
            'subject': subject, 'scenario': arm['scenario'], 'trial': arm['trial'],
            'target_rps': arm['target_rps'], 'requested_recovery_s': arm.get('requested_recovery_s'),
            'status': arm['status'], 'summary_file': summary.name, 'raw_file': raw.name,
        })
        self.manifest['saved_arms'] += 1
        self.manifest['observed_arms'] += int(arm['status'] == 'observed')
        self._save_manifest()
        print(f"  saved local arm {self.manifest['saved_arms']}/{self.manifest['planned_arms']}: {summary}", flush=True)

    def finish(self, status=None):
        self.manifest['status'] = status or (
            'complete' if self.manifest['observed_arms'] == self.manifest['planned_arms'] else 'incomplete')
        self._save_manifest()
