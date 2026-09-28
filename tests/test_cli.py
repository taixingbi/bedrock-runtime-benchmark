"""bedrock-benchmark CLI: names in, the existing engine underneath.
Everything runs against the fake Bedrock client -- no AWS calls."""
import contextlib
import io
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from bedrock_benchmark import cli
from bedrock_benchmark.client import BedrockConverseTarget

from .fakes import FakeBedrockRuntimeClient

REPO = Path(__file__).resolve().parents[1]

TINY = """\
purpose: characterization
name: tiny
workloads: [short_chat]
warmup_s: 0.02
duration_s: 0.1
stream: false
seed: 1
sweep: {type: concurrency, values: [1]}
"""


def _fake_factory(spec):
    return BedrockConverseTarget(model_id=spec.target.model_id, client=FakeBedrockRuntimeClient(chars_per_token=3.3))


class CliTests(unittest.TestCase):
    def setUp(self):
        self._cwd = os.getcwd()
        self._env = os.environ.pop("BEDROCK_BENCHMARK_HOME", None)

    def tearDown(self):
        os.chdir(self._cwd)
        if self._env is not None:
            os.environ["BEDROCK_BENCHMARK_HOME"] = self._env
        else:
            os.environ.pop("BEDROCK_BENCHMARK_HOME", None)

    def _main(self, *argv, **kwargs):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(list(argv), **kwargs)
        return code, out.getvalue()

    def test_list_shows_models_and_experiments_from_anywhere(self):
        with tempfile.TemporaryDirectory() as elsewhere:
            os.chdir(elsewhere)  # not inside the checkout -- found via the installed package
            code, out = self._main("list")
        self.assertEqual(code, 0)
        self.assertIn("nova-micro", out)
        for name in ("rate-capacity", "concurrency-sweep", "mixed-capacity", "workload-shape-calibration"):
            self.assertIn(name, out)

    def test_experiments_are_named_not_pathed(self):
        self.assertEqual(cli.experiment_paths(["rate-capacity"], REPO), [str(REPO / "experiments/rate-capacity.yaml")])
        self.assertEqual(len(cli.experiment_paths(["all"], REPO)), len(cli.experiment_names(REPO)))
        with self.assertRaises(SystemExit) as ctx:
            cli.experiment_paths(["experiments/rate-capacity.yaml"], REPO)
        self.assertIn("unknown experiment", str(ctx.exception))

    def test_plan_validates_and_sends_nothing(self):
        code, out = self._main("plan", "workload-shape-calibration", "--model", "nova-micro")
        self.assertEqual(code, 0)
        for text in ("Model:       nova-micro", "Region: us-east-1", "RPM 400", "Estimated duration", "No requests sent."):
            self.assertIn(text, out)
        self.assertEqual(self._main("run", "rate-capacity", "--model", "nova-micro", "--dry-run")[0], 0)

    def test_run_produces_the_existing_artifacts(self):
        """A throwaway checkout (BEDROCK_BENCHMARK_HOME) with one tiny
        experiment: `run` goes through the unchanged batch engine and
        writes capacity-profile.yaml + raw JSONL; `pilot` checks it."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "checkout"
            for d in ("catalog", "constraints"):
                shutil.copytree(REPO / d, home / d)
            (home / "experiments").mkdir()
            (home / "experiments" / "tiny.yaml").write_text(TINY)
            os.environ["BEDROCK_BENCHMARK_HOME"] = str(home)
            results = Path(tmp) / "out"

            code, out = self._main("run", "tiny", "--model", "nova-micro", "--results-dir", str(results),
                                   target_factory=_fake_factory)
            self.assertEqual(code, 0, out)
            model_dir = results / "nova-micro"
            self.assertEqual(len(list(model_dir.glob("tiny-*-capacity-profile.yaml"))), 1)
            self.assertEqual(len(list(model_dir.glob("tiny-*.jsonl"))), 1)
            self.assertTrue((results / "summary.yaml").exists())

            code, out = self._main("pilot", "tiny", "--model", "nova-micro", "--results-dir", str(Path(tmp) / "p"),
                                   target_factory=_fake_factory)
            self.assertIn("pilot:", out)
            self.assertTrue((Path(tmp) / "p" / "pilot.yaml").exists())


if __name__ == "__main__":
    unittest.main()
