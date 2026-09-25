import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

from bedrock_benchmark.batch import format_summary, plan, run_batch
from bedrock_benchmark.client import BedrockConverseTarget
from bedrock_benchmark.experiments.schema import load_experiment
from bedrock_benchmark.run_file import estimated_duration_s

from .fakes import FakeBedrockRuntimeClient


def _experiment(name: str, model_id: str = "m", **extra) -> dict:
    spec = {
        "name": name,
        "target": {"model_id": model_id},
        "workloads": [{"name": "short", "input_tokens": 100, "output_tokens": 16}],
        "sweep": {"type": "rate", "values": [20.0]},
        "slo": {"latency_p95_ms": 3000},
        "warmup_s": 0.02, "duration_s": 0.1, "stream": False, "seed": 1,
    }
    spec.update(extra)
    return spec


def _fake_factory(broken: set = frozenset()):
    def factory(spec):
        if spec.name in broken:
            raise RuntimeError(f"no model access for {spec.name}")
        return BedrockConverseTarget(model_id=spec.target.model_id, client=FakeBedrockRuntimeClient())
    return factory


class BatchTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, *specs) -> list:
        paths = []
        for spec in specs:
            path = self.dir / f"{spec['name']}.yaml"
            path.write_text(yaml.safe_dump(spec))
            paths.append(str(path))
        return paths

    def _run(self, paths, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return run_batch(paths, results_dir=self.dir / "out", **kwargs)

    def test_runs_every_experiment_and_writes_a_summary(self):
        paths = self._write(_experiment("a"), _experiment("b"))

        batch = self._run(paths, target_factory=_fake_factory())

        self.assertEqual([r.status for r in batch.results], ["ok", "ok"])
        self.assertEqual(batch.exit_code, 0)
        for r in batch.results:
            self.assertTrue(Path(r.profile_path).exists())
            self.assertTrue(Path(r.jsonl_path).exists())
            self.assertTrue(r.recommendations)
        summary = yaml.safe_load((self.dir / "out" / "summary.yaml").read_text())
        self.assertEqual([e["status"] for e in summary["experiments"]], ["ok", "ok"])
        self.assertIn("2/2 succeeded", format_summary(batch))

    def test_a_failed_experiment_does_not_stop_the_batch(self):
        paths = self._write(_experiment("a"), _experiment("broken"), _experiment("c"))

        batch = self._run(paths, target_factory=_fake_factory(broken={"broken"}))

        self.assertEqual([r.status for r in batch.results], ["ok", "failed", "ok"])
        self.assertIn("no model access", batch.results[1].error)
        self.assertEqual(batch.exit_code, 1)

    def test_fail_fast_skips_the_rest(self):
        paths = self._write(_experiment("broken"), _experiment("b"))

        batch = self._run(paths, target_factory=_fake_factory(broken={"broken"}), fail_fast=True)

        self.assertEqual([r.status for r in batch.results], ["failed", "skipped"])

    def test_an_invalid_file_fails_before_anything_runs(self):
        paths = self._write(_experiment("a"), _experiment("bad", repetitions=0))
        factory_calls = []

        def factory(spec):
            factory_calls.append(spec.name)
            return _fake_factory()(spec)

        with self.assertRaises(ValueError):
            self._run(paths, target_factory=factory)
        self.assertEqual(factory_calls, [])

    def test_gateway_diff_runs_over_all_produced_profiles(self):
        paths = self._write(_experiment("a", model_id="model-x"))

        batch = self._run(paths, target_factory=_fake_factory(), gateway_config={"models": {}})

        kinds = {f.kind for f in batch.gateway_diff.findings}
        self.assertIn("model_rpm_unset", kinds)  # warn -> non-zero exit
        self.assertEqual(batch.exit_code, 1)
        summary = yaml.safe_load((self.dir / "out" / "summary.yaml").read_text())
        self.assertIn("gateway_diff", summary)


class PlanTests(unittest.TestCase):
    def test_estimate_counts_subjects_points_repetitions_warmup_and_window(self):
        spec = load_experiment("experiments/token-sweep.yaml")  # 4 workloads x 4 points
        self.assertEqual(estimated_duration_s(spec), 4 * 4 * 1 * (10 + 90))
        mixed = load_experiment("experiments/mixed-capacity.yaml")  # a mix is ONE subject
        self.assertEqual(estimated_duration_s(mixed), 1 * 8 * 1 * (10 + 90))

    def test_plan_covers_every_shipped_experiment(self):
        paths = sorted(str(p) for p in Path("experiments").glob("*.yaml"))
        self.assertEqual(len(plan(paths)), len(paths))


class RunAllCliTests(unittest.TestCase):
    def test_dry_run_prints_the_plan_and_calls_nothing(self):
        proc = subprocess.run(
            [sys.executable, "scripts/run_all.py", "--dry-run"], capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("nova-micro-concurrency-sweep", proc.stdout)
        self.assertIn("run sequentially", proc.stdout)


if __name__ == "__main__":
    unittest.main()
