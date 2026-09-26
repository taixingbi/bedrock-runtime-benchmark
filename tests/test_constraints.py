import tempfile
import unittest
from pathlib import Path

import yaml

from bedrock_benchmark.constraints import load_quotas, load_slo, resolve_account
from bedrock_benchmark.experiments.schema import load_experiment
from bedrock_benchmark.models import ModelConfig, load_models
from bedrock_benchmark.workload import load_workloads

MICRO = ModelConfig(name="nova-micro", model_id="us.amazon.nova-micro-v1:0", quota_rpm=400, quota_tpm=8_000_000)


def _write(text: str) -> str:
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    f.write(text)
    f.close()
    return f.name


class ShippedConstraintsTests(unittest.TestCase):
    def test_slo_file_defines_the_three_profiles_on_ttft_and_tpot(self):
        slos = load_slo()
        self.assertEqual(set(slos.profiles), {"interactive_short", "interactive_medium", "long_generation"})
        for name, p in slos.profiles.items():
            with self.subTest(profile=name):
                self.assertIsNotNone(p.ttft_p95_ms)
                self.assertIsNotNone(p.tpot_p95_ms)
        short, medium, long_ = (slos.get(n) for n in ("interactive_short", "interactive_medium", "long_generation"))
        self.assertLess(short.ttft_p95_ms, medium.ttft_p95_ms)
        self.assertLess(medium.ttft_p95_ms, long_.ttft_p95_ms)
        self.assertLess(short.tpot_p95_ms, long_.tpot_p95_ms)

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

    def test_catalog_binds_every_workload_to_an_existing_profile(self):
        slos = load_slo()
        catalog = load_workloads()
        self.assertEqual(
            {n: w.slo_profile for n, w in catalog.items()},
            {"short_chat": "interactive_short", "rag_answer": "interactive_medium", "long_generation": "long_generation"},
        )
        for w in catalog.values():
            self.assertIn(w.slo_profile, slos.profiles)

    def test_experiments_only_list_catalog_workloads(self):
        catalog = load_workloads()
        for path in sorted(Path("experiments").glob("*.yaml")):
            raw = yaml.safe_load(path.read_text())
            with self.subTest(path=path.name):
                self.assertTrue(all(isinstance(n, str) and n in catalog for n in raw["workloads"]))


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
        path = _write("profiles:\n  interactive_short: {tpot_p95_ms: 5}\n"
                      "  interactive_medium: {tpot_p95_ms: 6}\n  long_generation: {tpot_p95_ms: 7}\n")
        try:
            spec = load_experiment("experiments/token-sweep.yaml", MICRO, slo_file=path)
            self.assertEqual(spec.slo_for("short_chat").tpot_p95_ms, 5)
            self.assertEqual(spec.slo_for("rag_answer").tpot_p95_ms, 6)
            self.assertEqual(spec.slo_for("long_generation").tpot_p95_ms, 7)
        finally:
            Path(path).unlink()

    def test_catalog_binding_a_profile_the_slo_file_lacks_is_rejected(self):
        path = _write("profiles: {interactive_short: {tpot_p95_ms: 1}}\n")
        try:
            with self.assertRaisesRegex(ValueError, "long_generation"):
                load_experiment("experiments/token-sweep.yaml", MICRO, slo_file=path)
        finally:
            Path(path).unlink()

    def test_blend_gate_is_the_strictest_among_used_profiles(self):
        path = _write(
            "profiles:\n"
            "  interactive_short: {tpot_p95_ms: 50, success_rate_min: 0.99, throttle_rate_max: 0.01}\n"
            "  interactive_medium: {tpot_p95_ms: 60, success_rate_min: 0.99, throttle_rate_max: 0.01}\n"
            "  long_generation: {tpot_p95_ms: 70, success_rate_min: 0.999, throttle_rate_max: 0.001, confidence: 0.95}\n"
        )
        try:
            gate = load_experiment("experiments/mixed-capacity.yaml", MICRO, slo_file=path).slo
            self.assertEqual((gate.success_rate_min, gate.throttle_rate_max, gate.confidence), (0.999, 0.001, 0.95))
            self.assertIsNone(gate.tpot_p95_ms)  # latency components always from each class's own profile
        finally:
            Path(path).unlink()


class ExperimentRejectsInlineConstraintsTests(unittest.TestCase):
    MINIMAL = (
        "name: minimal\n"
        "workloads: [short_chat]\n"
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

    def test_inline_workload_shape_is_rejected(self):
        path = _write(self.MINIMAL.replace("[short_chat]", "[{name: w, input_tokens: 1, output_tokens: 1}]"))
        try:
            with self.assertRaisesRegex(ValueError, "list of workload names"):
                load_experiment(path, MICRO)
        finally:
            Path(path).unlink()

    def test_catalog_workload_without_a_profile_is_rejected(self):
        catalog = _write("workloads: {w: {input_tokens: 1, output_tokens: 1}}\n")
        try:
            with self.assertRaisesRegex(ValueError, "explicit slo_profile"):
                load_workloads(catalog)
        finally:
            Path(catalog).unlink()

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
