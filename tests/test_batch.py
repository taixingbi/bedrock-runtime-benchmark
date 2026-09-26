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
        "workloads": ["short"],
        "sweep": {"type": "rate", "quota_fractions": [1.0]},
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
        # Non-streaming fakes have no TTFT, so a latency-only SLO file --
        # also exercises passing a custom SLO file through the batch.
        self.slo_file = self.dir / "slo.yaml"
        self.slo_file.write_text("profiles:\n  fast: {latency_p95_ms: 3000}\n")
        self.workloads_file = self.dir / "workloads.yaml"
        self.workloads_file.write_text("workloads:\n  short: {input_tokens: 100, output_tokens: 16, slo_profile: fast}\n")

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
            return run_batch(paths, models, results_dir=self.dir / "out", slo_file=str(self.slo_file),
                             workloads_file=str(self.workloads_file), **kwargs)

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

        # Non-streaming fakes can't statistically confirm a 0.1% throttle SLO
        # in a 0.1s window, so there's no production value to propose from --
        # the diff says so instead of comparing an unconfirmed number.
        self.assertIn("no_confirmed_envelope", {f.kind for f in batch.gateway_diff.findings})
        self.assertEqual(batch.exit_code, 1)
        self.assertIn("gateway_diff", yaml.safe_load((self.dir / "out" / "summary.yaml").read_text()))


class SloProfileFilterBatchTests(unittest.TestCase):
    def test_plan_marks_skips_and_the_dry_run_shows_them(self):
        paths = sorted(str(p) for p in Path("experiments").glob("*.yaml"))
        micro = load_models(names=["nova-micro"])
        planned = plan(paths, micro, only_slo_profiles={"gold"})
        skipped = {p.experiment: p.skip_reason for p in planned if p.skip_reason}
        self.assertEqual(set(skipped), {"mixed-capacity"})
        proc = subprocess.run(
            [sys.executable, "scripts/run_all.py", "--dry-run", "--model", "nova-micro", "--slo-profile", "gold"],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("skip", proc.stdout)
        self.assertIn("total: 3 runs", proc.stdout)

    def test_run_batch_runs_only_matching_workloads(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "slo.yaml").write_text("profiles:\n  gold: {latency_p95_ms: 3000}\n  bronze: {latency_p95_ms: 9000}\n")
            (d / "workloads.yaml").write_text(
                "workloads:\n"
                "  chat: {input_tokens: 100, output_tokens: 16, slo_profile: gold}\n"
                "  gen: {input_tokens: 100, output_tokens: 16, slo_profile: bronze}\n"
            )
            both = _experiment("both", workloads=["chat", "gen"])
            only_gen = _experiment("only_gen", workloads=["gen"])
            paths = []
            for spec in (both, only_gen):
                (d / f"{spec['name']}.yaml").write_text(yaml.safe_dump(spec))
                paths.append(str(d / f"{spec['name']}.yaml"))
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                batch = run_batch(paths, [FAST], results_dir=d / "out", slo_file=str(d / "slo.yaml"),
                                  workloads_file=str(d / "workloads.yaml"), target_factory=_fake_factory(),
                                  only_slo_profiles={"gold"})
            self.assertEqual([r.experiment for r in batch.results], ["both"])  # only_gen skipped
            profile = yaml.safe_load(Path(batch.results[0].profile_path).read_text())
            self.assertEqual(list(profile["workload_classes"]), ["chat"])


class PlanTests(unittest.TestCase):
    def test_estimate_counts_subjects_points_repetitions_warmup_and_window(self):
        micro = load_models(names=["nova-micro"])[0]
        spec = load_experiment("experiments/token-sweep.yaml", micro)  # 3 workloads x 4 points
        self.assertEqual(estimated_duration_s(spec), 3 * 4 * 1 * (10 + 90))
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
