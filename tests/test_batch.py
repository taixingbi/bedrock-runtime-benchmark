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
from bedrock_benchmark.models import ModelConfig, load_models
from bedrock_benchmark.run_file import estimated_duration_s

from .fakes import FakeBedrockRuntimeClient

# 1200 RPM = 20 rps at 1.0x quota -- enough requests in a 0.1s window.
FAST = ModelConfig(name="fast", model_id="m.fast-v1:0", quota_rpm=1200, quota_tpm=10**8)
OTHER = ModelConfig(name="other", model_id="m.other-v1:0", quota_rpm=1200, quota_tpm=10**8)


def _experiment(name: str, **extra) -> dict:
    spec = {
        "name": name,
        "workloads": [{"name": "short", "input_tokens": 100, "output_tokens": 16}],
        "sweep": {"type": "rate", "quota_fractions": [1.0]},
        "slo": {"latency_p95_ms": 3000},
        "warmup_s": 0.02, "duration_s": 0.1, "stream": False, "seed": 1,
    }
    spec.update(extra)
    return spec


def _fake_factory(broken: set = frozenset()):
    """broken: {(model_name, experiment_name)} pairs whose target fails."""
    def factory(spec):
        if (spec.model_name, spec.name) in broken:
            raise RuntimeError(f"no model access for {spec.model_name}")
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

    def _run(self, paths, models, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return run_batch(paths, models, results_dir=self.dir / "out", **kwargs)

    def test_runs_every_model_x_experiment_into_per_model_folders(self):
        paths = self._write(_experiment("a"), _experiment("b"))

        batch = self._run(paths, [FAST, OTHER], target_factory=_fake_factory())

        self.assertEqual([(r.model, r.experiment) for r in batch.results],
                         [("fast", "a"), ("fast", "b"), ("other", "a"), ("other", "b")])
        self.assertEqual({r.status for r in batch.results}, {"ok"})
        self.assertEqual(batch.exit_code, 0)
        for r in batch.results:
            self.assertEqual(Path(r.profile_path).parent, self.dir / "out" / r.model)
            self.assertTrue(Path(r.jsonl_path).exists())
            profile = yaml.safe_load(Path(r.profile_path).read_text())
            self.assertEqual((profile["experiment"], profile["model"]["name"]), (r.experiment, r.model))
            self.assertEqual(profile["sweep"]["quota_fractions"], [1.0])
        summary = yaml.safe_load((self.dir / "out" / "summary.yaml").read_text())
        self.assertEqual(len(summary["runs"]), 4)
        self.assertIn("4/4 succeeded", format_summary(batch))

    def test_a_failed_run_does_not_stop_the_batch(self):
        paths = self._write(_experiment("a"))

        batch = self._run(paths, [FAST, OTHER], target_factory=_fake_factory(broken={("fast", "a")}))

        self.assertEqual([r.status for r in batch.results], ["failed", "ok"])
        self.assertIn("no model access", batch.results[0].error)
        self.assertEqual(batch.exit_code, 1)

    def test_fail_fast_skips_the_rest(self):
        paths = self._write(_experiment("a"))

        batch = self._run(paths, [FAST, OTHER], target_factory=_fake_factory(broken={("fast", "a")}), fail_fast=True)

        self.assertEqual([r.status for r in batch.results], ["failed", "skipped"])

    def test_every_pair_is_validated_before_anything_runs(self):
        """A quota-relative sweep on a model with no quota must fail up
        front, not after the other models' runs."""
        paths = self._write(_experiment("a"))
        no_quota = ModelConfig(name="noquota", model_id="m.nq-v1:0")
        calls = []

        def factory(spec):
            calls.append(spec.model_name)
            return _fake_factory()(spec)

        with self.assertRaisesRegex(ValueError, "quota.rpm"):
            self._run(paths, [FAST, no_quota], target_factory=factory)
        self.assertEqual(calls, [])

    def test_gateway_diff_runs_over_all_produced_profiles(self):
        paths = self._write(_experiment("a"))

        batch = self._run(paths, [FAST], target_factory=_fake_factory(), gateway_config={"models": {}})

        self.assertIn("model_rpm_unset", {f.kind for f in batch.gateway_diff.findings})
        self.assertEqual(batch.exit_code, 1)
        self.assertIn("gateway_diff", yaml.safe_load((self.dir / "out" / "summary.yaml").read_text()))


class PlanTests(unittest.TestCase):
    def test_estimate_counts_subjects_points_repetitions_warmup_and_window(self):
        micro = load_models(names=["nova-micro"])[0]
        spec = load_experiment("experiments/token-sweep.yaml", micro)  # 4 workloads x 4 points
        self.assertEqual(estimated_duration_s(spec), 4 * 4 * 1 * (10 + 90))
        mixed = load_experiment("experiments/mixed-capacity.yaml", micro)  # a mix is ONE subject
        self.assertEqual(estimated_duration_s(mixed), 1 * 8 * 1 * (10 + 90))

    def test_plan_is_every_experiment_for_every_enabled_model_grouped_by_model(self):
        paths = sorted(str(p) for p in Path("experiments").glob("*.yaml"))
        models = load_models()
        planned = plan(paths, models)
        self.assertEqual(len(planned), len(paths) * len(models))
        self.assertEqual([p.model.name for p in planned[: len(paths)]], [models[0].name] * len(paths))


class CliTests(unittest.TestCase):
    def test_run_all_dry_run_prints_the_plan_and_calls_nothing(self):
        proc = subprocess.run([sys.executable, "scripts/run_all.py", "--dry-run"], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for name in ("nova-micro", "qwen3-32b", "rate-capacity", "run sequentially"):
            self.assertIn(name, proc.stdout)

    def test_model_filter(self):
        proc = subprocess.run(
            [sys.executable, "scripts/run_all.py", "--dry-run", "--model", "nova-pro"], capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("nova-pro", proc.stdout)
        self.assertNotIn("nova-micro", proc.stdout)


if __name__ == "__main__":
    unittest.main()
