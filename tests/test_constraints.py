import tempfile
import unittest
from pathlib import Path

from bedrock_benchmark.constraints import load_quotas, load_slo
from bedrock_benchmark.experiments.schema import load_experiment
from bedrock_benchmark.models import ModelConfig, load_models

MICRO = ModelConfig(name="nova-micro", model_id="us.amazon.nova-micro-v1:0", quota_rpm=400, quota_tpm=8_000_000)


def _write(text: str) -> str:
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    f.write(text)
    f.close()
    return f.name


class ShippedConstraintsTests(unittest.TestCase):
    def test_slo_file_defines_interactive_default_and_long_generation(self):
        slos = load_slo()
        self.assertEqual(slos.default, "interactive")
        self.assertEqual(slos.get(None).latency_p95_ms, 3000)
        self.assertEqual(slos.get("long_generation").latency_p95_ms, 10000)
        self.assertEqual(slos.get("long_generation").ttft_p95_ms, slos.get(None).ttft_p95_ms)

    def test_quota_file_covers_every_shipped_model(self):
        quotas = load_quotas()
        for m in load_models(include_disabled=True):
            with self.subTest(model=m.name):
                self.assertIn(m.name, quotas)
                self.assertTrue(m.quota_rpm and m.quota_tpm)

    def test_no_experiment_defines_its_own_slo(self):
        """Every experiment gets SLOs only from constraints/slo.yaml --
        the same workload class is judged identically everywhere."""
        for path in sorted(Path("experiments").glob("*.yaml")):
            spec = load_experiment(str(path), MICRO)
            with self.subTest(path=path.name):
                self.assertEqual(spec.slo, load_slo().get(None))
                for w in spec.workloads:
                    self.assertEqual(spec.slo_for(w.name), load_slo().get(w.slo_profile))


class SloFileTests(unittest.TestCase):
    def test_invalid_slo_files(self):
        bad = [
            "profiles: {a: {latency_p95_ms: 1}}\n",                      # no default
            "default: b\nprofiles: {a: {latency_p95_ms: 1}}\n",         # default not a profile
            "default: a\nprofiles: {}\n",                               # no profiles
            "default: a\nprofiles: {a: {confidence: 1.5}}\n",
            "default: a\nprofiles: {a: {throttle_rate_max: 2}}\n",
            "default: a\nprofiles: {a: {latency_p95: 1}}\n",            # typo'd key
        ]
        for text in bad:
            path = _write(text)
            try:
                with self.subTest(text=text), self.assertRaises((ValueError, TypeError)):
                    load_slo(path)
            finally:
                Path(path).unlink()

    def test_custom_slo_file_is_used_by_experiments(self):
        path = _write("default: strict\nprofiles:\n  strict: {latency_p95_ms: 500}\n  long_generation: {latency_p95_ms: 900}\n")
        try:
            spec = load_experiment("experiments/token-sweep.yaml", MICRO, slo_file=path)
            self.assertEqual(spec.slo_for("short_short").latency_p95_ms, 500)
            self.assertEqual(spec.slo_for("long_long").latency_p95_ms, 900)
            self.assertEqual(spec.slo_default, "strict")
        finally:
            Path(path).unlink()

    def test_experiment_naming_a_profile_the_slo_file_lacks_is_rejected(self):
        path = _write("default: only\nprofiles: {only: {latency_p95_ms: 1}}\n")
        try:
            with self.assertRaisesRegex(ValueError, "long_generation"):
                load_experiment("experiments/token-sweep.yaml", MICRO, slo_file=path)
        finally:
            Path(path).unlink()


class ExperimentRejectsInlineConstraintsTests(unittest.TestCase):
    MINIMAL = (
        "name: minimal\n"
        "workloads: [{name: w, input_tokens: 100, output_tokens: 16}]\n"
        "sweep: {type: concurrency, values: [1]}\n"
    )

    def test_inline_slo_is_rejected(self):
        for extra in ("slo: {latency_p95_ms: 1}\n", "slo_profiles: {x: {latency_p95_ms: 1}}\n"):
            path = _write(self.MINIMAL + extra)
            try:
                with self.subTest(extra=extra), self.assertRaisesRegex(ValueError, "defined once"):
                    load_experiment(path, MICRO)
            finally:
                Path(path).unlink()

    def test_inline_quota_is_rejected(self):
        path = _write(self.MINIMAL + "quota: {rpm: 1}\n")
        try:
            with self.assertRaisesRegex(ValueError, "constraints/quota.yaml"):
                load_experiment(path, MICRO)
        finally:
            Path(path).unlink()


class QuotaFileTests(unittest.TestCase):
    def test_parses_and_validates(self):
        path = _write("x: {rpm: 10, tpm: 100}\ny: {rpm: 5, output_burndown: 0}\n")
        try:
            with self.assertRaisesRegex(ValueError, "output_burndown"):
                load_quotas(path)
        finally:
            Path(path).unlink()


if __name__ == "__main__":
    unittest.main()
