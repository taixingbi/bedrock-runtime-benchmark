import unittest

from bedrock_benchmark.ceiling import provider_ceiling
from bedrock_benchmark.experiments.schema import load_experiment
from bedrock_benchmark.models import ModelConfig
from bedrock_benchmark.workload import WorkloadProfile

SHORT = WorkloadProfile(name="short", input_tokens=512, output_tokens=64)
LONG = WorkloadProfile(name="long", input_tokens=4096, output_tokens=512)


class ProviderCeilingTests(unittest.TestCase):
    def test_short_workload_is_rpm_bound(self):
        c = provider_ceiling([(SHORT, 1.0)], rpm=400, tpm=8_000_000)
        self.assertEqual(c.tokens_per_request, 576)
        self.assertAlmostEqual(c.rpm_rps, 6.6667, places=3)
        self.assertAlmostEqual(c.tpm_rps, 8_000_000 / 576 / 60, places=3)
        self.assertEqual(c.binding, "rpm")
        self.assertAlmostEqual(c.rps, c.rpm_rps)

    def test_long_workload_on_a_tight_tpm_quota_is_tpm_bound(self):
        """The reviewer's case: 4096 + 512 tokens/request on a quota
        whose TPM runs out before its RPM does."""
        c = provider_ceiling([(LONG, 1.0)], rpm=200, tpm=600_000)
        self.assertEqual(c.tokens_per_request, 4608)
        self.assertAlmostEqual(c.tpm_rps, 600_000 / 4608 / 60, places=4)  # ~2.17 rps
        self.assertEqual(c.binding, "tpm")
        self.assertLess(c.rps, 200 / 60)

    def test_mix_uses_share_weighted_tokens_per_request(self):
        c = provider_ceiling([(SHORT, 0.7), (LONG, 0.3)], rpm=400, tpm=8_000_000)
        self.assertAlmostEqual(c.tokens_per_request, 0.7 * 576 + 0.3 * 4608)

    def test_output_burndown_multiplies_output_tokens(self):
        c = provider_ceiling([(SHORT, 1.0)], rpm=400, tpm=8_000_000, output_burndown=5)
        self.assertEqual(c.tokens_per_request, 512 + 64 * 5)

    def test_unknown_tpm_falls_back_to_rpm_and_unknown_both_is_none(self):
        self.assertEqual(provider_ceiling([(SHORT, 1.0)], rpm=400, tpm=None).binding, "rpm")
        none = provider_ceiling([(SHORT, 1.0)], rpm=None, tpm=None)
        self.assertIsNone(none.rps)
        self.assertIsNone(none.binding)


class QuotaRelativeSweepTests(unittest.TestCase):
    def test_rate_sweep_resolves_against_the_tpm_ceiling_when_tpm_binds(self):
        tight_tpm = ModelConfig(name="t", model_id="m.t-v1:0", quota_rpm=10_000, quota_tpm=600_000)
        spec = load_experiment("experiments/rate-capacity.yaml", tight_tpm)  # short: 576 tokens/request
        ceiling = spec.provider_ceilings["short"]
        self.assertEqual(ceiling.binding, "tpm")
        i = spec.sweep.quota_fractions.index(1.0)
        self.assertAlmostEqual(spec.sweep_values("short")[i], round(600_000 / 576 / 60, 4))

    def test_tpm_only_quota_is_enough_for_a_relative_sweep(self):
        tpm_only = ModelConfig(name="t", model_id="m.t-v1:0", quota_tpm=600_000)
        spec = load_experiment("experiments/rate-capacity.yaml", tpm_only)
        self.assertEqual(spec.provider_ceilings["short"].binding, "tpm")


if __name__ == "__main__":
    unittest.main()
