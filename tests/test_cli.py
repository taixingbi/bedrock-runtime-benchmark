"""bedrock-benchmark CLI: names in, the existing engine underneath.
Everything runs against the fake Bedrock client -- no AWS calls."""
import contextlib
import hashlib
import io
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import yaml

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
        for name in ("capacity-reference-rate", "capacity-reference-concurrency", "capacity-mix-rate", "capacity-shape-concurrency"):
            self.assertIn(name, out)

    def test_experiments_are_named_not_pathed(self):
        self.assertEqual(cli.experiment_paths(["capacity-reference-rate"], REPO), [str(REPO / "experiments/capacity-reference-rate.yaml")])
        self.assertEqual(len(cli.experiment_paths(["all"], REPO)), len(cli.experiment_names(REPO)))
        with self.assertRaises(SystemExit) as ctx:
            cli.experiment_paths(["experiments/capacity-reference-rate.yaml"], REPO)
        self.assertIn("unknown experiment", str(ctx.exception))

    def test_plan_validates_and_sends_nothing(self):
        code, out = self._main("plan", "capacity-shape-concurrency", "--model", "nova-micro")
        self.assertEqual(code, 0)
        for text in ("Model:       nova-micro", "Region: us-east-1", "RPM 400", "Estimated duration", "No requests sent."):
            self.assertIn(text, out)
        self.assertEqual(self._main("run", "capacity-reference-rate", "--model", "nova-micro", "--dry-run")[0], 0)

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

    def test_expensive_commands_need_an_explicit_model_scope(self):
        for command in ("plan", "pilot", "run"):
            with self.assertRaises(SystemExit) as ctx:
                self._main(command, "capacity-reference-rate")
            self.assertIn("--all-models", str(ctx.exception))
        code, out = self._main("plan", "capacity-reference-rate", "--all-models")
        self.assertEqual(code, 0)
        self.assertIn("No requests sent.", out)

    def test_paved_road_run_summary_validate_publish(self):
        """run (with ownership) -> the profile carries `run:` and conforms to
        the JSON Schema -> summary -> validate writes a temporal profile ->
        publish copies the run with a sha256 manifest, once."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "checkout"
            for d in ("catalog", "constraints"):
                shutil.copytree(REPO / d, home / d)
            (home / "experiments").mkdir()
            (home / "experiments" / "tiny.yaml").write_text(TINY)
            os.environ["BEDROCK_BENCHMARK_HOME"] = str(home)
            results = Path(tmp) / "results" / "run-1"

            code, out = self._main("run", "tiny", "--model", "nova-micro", "--results-dir", str(results),
                                   "--owner", "alice", "--ticket", "CAP-1", "--purpose", "onboarding",
                                   target_factory=_fake_factory)
            self.assertEqual(code, 0, out)
            self.assertIn("Can I trust this run?", out)
            self.assertIn("Production usable?         NO", out)
            profile_path = next((results / "nova-micro").glob("tiny-*-capacity-profile.yaml"))
            run = yaml.safe_load(profile_path.read_text())["run"]
            self.assertEqual({k: run[k] for k in ("owner", "ticket", "purpose", "environment")},
                             {"owner": "alice", "ticket": "CAP-1", "purpose": "onboarding", "environment": "dev"})
            self.assertTrue(run["run_id"])

            code, out = self._main("validate-profile", str(profile_path))
            self.assertEqual(code, 0, out)
            self.assertIn("capacity-profile-v23.json", out)

            code, out = self._main("summary", str(results))
            self.assertEqual(code, 0)
            self.assertIn("RESULT  nova-micro / tiny", out)

            for _ in range(2):  # a second validate must not read the first one's output as a run
                code, out = self._main("validate", str(results.parent))
                self.assertEqual(code, 0, out)
                self.assertIn("(1 profiles;", out)
            temporal = results.parent / "temporal-capacity-profile.yaml"
            doc = yaml.safe_load(temporal.read_text())
            self.assertEqual(doc["artifact"], "temporal_capacity_profile")
            self.assertTrue(all(e["status"] == "INSUFFICIENT_EVIDENCE" for e in doc["entries"]))
            self.assertEqual(self._main("validate-profile", str(temporal))[0], 0)

            shared = Path(tmp) / "shared"
            code, out = self._main("publish", str(results), "--destination", str(shared))
            self.assertEqual(code, 0, out)
            manifest_path = next(shared.rglob("manifest.yaml"))
            manifest = yaml.safe_load(manifest_path.read_text())
            self.assertEqual((manifest["owner"], manifest["ticket"]), ("alice", "CAP-1"))
            self.assertEqual(manifest["evidence"], "single_run_operating_envelope")
            for f in manifest["files"]:
                data = (manifest_path.parent / f["path"]).read_bytes()
                self.assertEqual(hashlib.sha256(data).hexdigest(), f["sha256"])
            code, out = self._main("publish", str(results), "--destination", str(shared))
            self.assertEqual(code, 1)
            self.assertIn("immutable", out)

            s3 = FakeS3()
            code, out = self._main("publish", str(results), "--destination", "s3://bucket/capacity", s3_client=s3)
            self.assertEqual(code, 0, out)
            self.assertTrue(s3.keys[-1].startswith("capacity/") and s3.keys[-1].endswith("/manifest.yaml"))
            self.assertEqual(len(s3.keys), len(manifest["files"]) + 1)

    def test_validate_profile_rejects_a_broken_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "x-capacity-profile.yaml"
            bad.write_text(yaml.safe_dump({"schema_version": 23, "experiment": "x", "purpose": "production"}))
            code, out = self._main("validate-profile", str(bad))
            self.assertEqual(code, 1)
            self.assertIn("purpose", out)
            bad.write_text(yaml.safe_dump({"schema_version": 7}))
            self.assertEqual(self._main("validate-profile", str(bad))[0], 1)


class FakeS3:
    def __init__(self):
        self.keys = []

    def list_objects_v2(self, Bucket, Prefix):
        return {"KeyCount": sum(k == Prefix for k in self.keys)}

    def upload_file(self, filename, bucket, key):
        self.keys.append(key)

    def put_object(self, Bucket, Key, Body):
        self.keys.append(Key)


class _Sts:
    def __init__(self, account):
        self.account = account

    def get_caller_identity(self):
        return {"Account": self.account, "Arn": f"arn:aws:iam::{self.account}:user/tester"}


class DoctorTests(unittest.TestCase):
    def setUp(self):
        from bedrock_benchmark.models import load_models
        self.model = load_models(str(REPO / "catalog/models.yaml"), names=["nova-micro"],
                                 quota_file=str(REPO / "constraints/quota.yaml"))[0]
        self._cwd = os.getcwd()

    def tearDown(self):
        os.chdir(self._cwd)

    def _doctor(self, *, account=None, runtime=None, live_quota=None):
        from bedrock_benchmark.quota import QuotaInfo
        m = self.model
        clients = dict(
            sts_client=_Sts(account or m.account), git=lambda args: "abc123\n" if "rev-parse" in args else "",
            runtime_client=runtime or FakeBedrockRuntimeClient(chars_per_token=3.3, count_tokens_supported=True),
            quota_fetch=lambda model_id, region: live_quota or QuotaInfo(m.quota_rpm, m.quota_tpm, "service_quotas"),
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["doctor", "--model", "nova-micro", "--account", m.account], aws_clients=clients)
        return code, out.getvalue()

    def test_ready(self):
        code, out = self._doctor()
        self.assertEqual(code, 0, out)
        self.assertIn("READY", out)
        for area in ("python", "boto3", "git", "slo.yaml", "experiments", "identity", "quota", "model access",
                     "streaming", "token counting"):
            self.assertIn(area, out)
        self.assertIn("count_tokens", out)

    def test_not_ready_names_what_to_fix(self):
        from bedrock_benchmark.quota import QuotaInfo
        denied = Exception("not authorized")
        denied.response = {"Error": {"Code": "AccessDeniedException"}}
        code, out = self._doctor(runtime=FakeBedrockRuntimeClient(error=denied))
        self.assertEqual(code, 1)
        self.assertIn("NOT READY -- fix: model access", out)
        self.assertIn("AccessDeniedException", out)

        code, out = self._doctor(live_quota=QuotaInfo(1.0, 1.0, "service_quotas"))
        self.assertEqual(code, 1)
        self.assertIn("NOT READY -- fix: quota", out)

        code, out = self._doctor(account="999999999999")
        self.assertEqual(code, 1)
        self.assertIn("identity", out.splitlines()[-1])

    def test_token_counting_falls_back_to_converse_usage(self):
        code, out = self._doctor(runtime=FakeBedrockRuntimeClient(chars_per_token=3.3))
        self.assertEqual(code, 0, out)
        self.assertIn("converse_usage", out)


if __name__ == "__main__":
    unittest.main()
