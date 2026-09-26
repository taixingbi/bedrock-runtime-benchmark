import tempfile
import unittest
from pathlib import Path

from bedrock_benchmark.constraints import load_quotas, load_slo, resolve_account
from bedrock_benchmark.experiments.schema import load_experiment
from bedrock_benchmark.models import ModelConfig, load_models

MICRO = ModelConfig(name="nova-micro", model_id="us.amazon.nova-micro-v1:0", quota_rpm=400, quota_tpm=8_000_000)


def _write(text: str) -> str:
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    f.write(text)
    f.close()
    return f.name


class ShippedConstraintsTests(unittest.TestCase):
    def test_slo_file_defines_three_tiers_and_no_default(self):
        slos = load_slo()
        self.assertEqual(set(slos.profiles), {"tier1_interactive", "tier2_standard", "tier3_throughput"})
        t1, t2, t3 = (slos.get(n) for n in ("tier1_interactive", "tier2_standard", "tier3_throughput"))
        # Each tier is strictly more relaxed than the one above it.
        self.assertLess(t1.ttft_p95_ms, t2.ttft_p95_ms)
        self.assertLess(t2.ttft_p95_ms, t3.ttft_p95_ms)
        self.assertLess(t1.latency_p95_ms, t2.latency_p95_ms)
        self.assertLess(t2.latency_p95_ms, t3.latency_p95_ms)
        self.assertLess(t1.throttle_rate_max, t2.throttle_rate_max)
        self.assertLess(t2.throttle_rate_max, t3.throttle_rate_max)
        self.assertGreaterEqual(t1.success_rate_min, t2.success_rate_min)

    def test_quota_file_covers_every_shipped_model_for_the_account_and_its_region(self):
        table = load_quotas()
        self.assertEqual(resolve_account(table, None), "646821141010")
        for m in load_models(include_disabled=True):
            with self.subTest(model=m.name):
                self.assertIn(("646821141010", m.region, m.name), table)
                self.assertTrue(m.quota_rpm and m.quota_tpm)

    def test_every_workload_names_its_profile_and_gets_exactly_it(self):
        slos = load_slo()
        for path in sorted(Path("experiments").glob("*.yaml")):
            spec = load_experiment(str(path), MICRO)
            for w in spec.workloads:
                with self.subTest(path=path.name, workload=w.name):
                    self.assertIn(w.slo_profile, slos.profiles)
                    self.assertEqual(spec.slo_for(w.name), slos.get(w.slo_profile))

    def test_workloads_bind_tiers_by_request_class(self):
        """Tier = business request class, from the workload's shape:
        short chat -> 1, long context + short answer (RAG) -> 2,
        long generation -> 3."""
        def expected(w):
            if w.output_tokens >= 512:
                return "tier3_throughput"
            return "tier2_standard" if w.input_tokens >= 4096 else "tier1_interactive"

        for path in sorted(Path("experiments").glob("*.yaml")):
            for w in load_experiment(str(path), MICRO).workloads:
                with self.subTest(path=path.name, workload=w.name):
                    self.assertEqual(w.slo_profile, expected(w))


class SloFileTests(unittest.TestCase):
    def test_invalid_slo_files(self):
        bad = [
            "default: a\nprofiles: {a: {latency_p95_ms: 1}}\n",   # no implicit default allowed
            "profiles: {}\n",
            "profiles: {a: {confidence: 1.5}}\n",
            "profiles: {a: {throttle_rate_max: 2}}\n",
            "profiles: {a: {latency_p95: 1}}\n",                   # typo'd key
        ]
        for text in bad:
            path = _write(text)
            try:
                with self.subTest(text=text), self.assertRaises((ValueError, TypeError)):
                    load_slo(path)
            finally:
                Path(path).unlink()

    def test_custom_slo_file_is_used_by_experiments(self):
        path = _write("profiles:\n  tier1_interactive: {latency_p95_ms: 500}\n"
                      "  tier2_standard: {latency_p95_ms: 700}\n  tier3_throughput: {latency_p95_ms: 900}\n")
        try:
            spec = load_experiment("experiments/token-sweep.yaml", MICRO, slo_file=path)
            self.assertEqual(spec.slo_for("short_short").latency_p95_ms, 500)
            self.assertEqual(spec.slo_for("long_input_short_output").latency_p95_ms, 700)
            self.assertEqual(spec.slo_for("long_long").latency_p95_ms, 900)
        finally:
            Path(path).unlink()

    def test_experiment_naming_a_profile_the_slo_file_lacks_is_rejected(self):
        path = _write("profiles: {tier1_interactive: {latency_p95_ms: 1}}\n")
        try:
            with self.assertRaisesRegex(ValueError, "tier3_throughput"):
                load_experiment("experiments/token-sweep.yaml", MICRO, slo_file=path)
        finally:
            Path(path).unlink()

    def test_blend_gate_is_the_strictest_among_used_profiles(self):
        path = _write(
            "profiles:\n"
            "  tier1_interactive: {latency_p95_ms: 3000, success_rate_min: 0.99, throttle_rate_max: 0.01}\n"
            "  tier3_throughput: {latency_p95_ms: 10000, success_rate_min: 0.999, throttle_rate_max: 0.001, confidence: 0.95}\n"
        )
        try:
            gate = load_experiment("experiments/mixed-capacity.yaml", MICRO, slo_file=path).slo
            self.assertEqual((gate.success_rate_min, gate.throttle_rate_max, gate.confidence), (0.999, 0.001, 0.95))
            self.assertIsNone(gate.latency_p95_ms)  # latency always from each class's own profile
        finally:
            Path(path).unlink()


class ExperimentRejectsInlineConstraintsTests(unittest.TestCase):
    MINIMAL = (
        "name: minimal\n"
        "workloads: [{name: w, input_tokens: 100, output_tokens: 16, slo_profile: tier1_interactive}]\n"
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

    def test_workload_without_a_profile_is_rejected(self):
        path = _write(self.MINIMAL.replace(", slo_profile: tier1_interactive", ""))
        try:
            with self.assertRaisesRegex(ValueError, "explicit `slo_profile:`"):
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
    def test_invalid_quota_files(self):
        bad = [
            "nova-micro: {rpm: 400}\n",                                        # not account-scoped
            'accounts: {"1234": {us-east-1: {x: {rpm: 1}}}}\n',                # not a 12-digit account
            'accounts: {"111111111111": {us-east-1: {x: {rpm: 1, output_burndown: 0}}}}\n',
        ]
        for text in bad:
            path = _write(text)
            try:
                with self.subTest(text=text), self.assertRaises(ValueError):
                    load_quotas(path)
            finally:
                Path(path).unlink()

    def test_unquoted_account_id_still_parses(self):
        path = _write("accounts: {111111111111: {us-east-1: {x: {rpm: 1}}}}\n")
        try:
            self.assertIn(("111111111111", "us-east-1", "x"), load_quotas(path))
        finally:
            Path(path).unlink()


if __name__ == "__main__":
    unittest.main()
