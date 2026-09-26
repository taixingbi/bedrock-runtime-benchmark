import unittest

from bedrock_benchmark.analysis.capacity import Check, Recommendation, SweepPoint, Verdict
from bedrock_benchmark.ceiling import ProviderCeiling
from bedrock_benchmark.experiments.executor import ExperimentReport, ProfileReport
from bedrock_benchmark.recommendation import SOURCE, admission_envelope
from bedrock_benchmark.client import TransportConfig
from bedrock_benchmark.experiments.schema import ExperimentSpec, SloConfig, SweepConfig, TargetConfig
from bedrock_benchmark.report import build_capacity_profile
from bedrock_benchmark.workload import WorkloadProfile

from .test_report import _metrics


def _spec(sweep_type: str) -> ExperimentSpec:
    return ExperimentSpec(
        name="t", target=TargetConfig(model_id="m"), sweep=SweepConfig(type=sweep_type, values=[1, 2, 4, 6, 8]),
        workloads=[WorkloadProfile(name="short", input_tokens=512, output_tokens=64)],
        slo=SloConfig(ttft_p95_ms=1000), provider_headroom=0.20, quota_headroom=0.10, transport=TransportConfig(),
    )

GATEWAY_KEYS = ("global_max_concurrency", "tenant_max_concurrency", "tenant", "rpm_limit", "queue",
                "aimd", "allocation", "global_max", "default_tenant_max")


class AdmissionEnvelopeTests(unittest.TestCase):
    """recommendation.admission_envelope: the statistically confirmed point
    after safety headroom -- fail closed, nothing gateway-specific."""

    def test_confirmed_concurrency(self):
        env = admission_envelope("concurrency", 5, headroom=0.2)["admission_envelope"]
        self.assertEqual(env, {"max_inflight": 4, "sustained_rps": None, "source": SOURCE,
                               "headroom_fraction": 0.2, "basis": {"statistically_confirmed_concurrency": 5}})

    def test_max_inflight_is_floored_to_an_integer(self):
        self.assertEqual(admission_envelope("concurrency", 6, headroom=0.2)["admission_envelope"]["max_inflight"], 4)
        self.assertEqual(admission_envelope("concurrency", 7, headroom=0.2)["admission_envelope"]["max_inflight"], 5)
        self.assertEqual(admission_envelope("concurrency", 5, headroom=0.0)["admission_envelope"]["max_inflight"], 5)
        self.assertIsInstance(admission_envelope("concurrency", 8, headroom=0.25)["admission_envelope"]["max_inflight"], int)

    def test_concurrency_flooring_to_zero_recommends_nothing(self):
        rec = admission_envelope("concurrency", 1, headroom=0.2)  # floor(0.8) = 0
        self.assertIsNone(rec["admission_envelope"])
        self.assertIn("< 1 in-flight", rec["reason"])

    def test_confirmed_rps(self):
        env = admission_envelope("rate", 5.0, headroom=0.2, quota_headroom=0.1,
                                 provider_ceiling_rps=6.6667)["admission_envelope"]
        self.assertEqual((env["max_inflight"], env["sustained_rps"], env["source"]), (None, 4.0, SOURCE))
        self.assertEqual(env["basis"], {"statistically_confirmed_offered_rps": 5.0, "provider_ceiling_rps": 6.6667})

    def test_measurement_bound_rps(self):
        env = admission_envelope("rate", 5.0, headroom=0.2, quota_headroom=0.1,
                                 provider_ceiling_rps=6.6667)["admission_envelope"]
        self.assertEqual((env["sustained_rps"], env["binding"]), (4.0, "measurement"))  # 4.0 < 6.0

    def test_quota_bound_rps(self):
        """Confirmed at 1.25x quota (burst allowance): 8.3333 x 0.8 = 6.67
        would exceed the quota; 6.6667 x 0.9 = 6.0 binds."""
        env = admission_envelope("rate", 8.3333, headroom=0.2, quota_headroom=0.1,
                                 provider_ceiling_rps=6.6667)["admission_envelope"]
        self.assertEqual((env["sustained_rps"], env["binding"]), (6.0, "provider_quota"))

    def test_rate_without_a_known_ceiling_is_measurement_bound(self):
        env = admission_envelope("rate", 5.0, headroom=0.2)["admission_envelope"]
        self.assertEqual((env["sustained_rps"], env["binding"]), (4.0, "measurement"))

    def test_no_confirmed_safe_point(self):
        for sweep in ("rate", "concurrency"):
            rec = admission_envelope(sweep, None, headroom=0.2, quota_headroom=0.1, provider_ceiling_rps=6.6667)
            with self.subTest(sweep=sweep):
                self.assertIsNone(rec["admission_envelope"])
                self.assertIn("INCONCLUSIVE", rec["reason"])

    def test_no_gateway_specific_settings(self):
        for rec in (admission_envelope("concurrency", 6, headroom=0.2),
                    admission_envelope("rate", 5.0, headroom=0.2, quota_headroom=0.1, provider_ceiling_rps=6.6667)):
            keys = str(rec).lower()
            for bad in GATEWAY_KEYS:
                with self.subTest(key=bad):
                    self.assertNotIn(bad, keys)


class ProfileRecommendationTests(unittest.TestCase):
    """End to end through build_capacity_profile."""

    def _profile(self, sweep_type, rec):
        spec = _spec(sweep_type)
        spec.provider_ceilings = {"short": ProviderCeiling(tokens_per_request=576, rpm_rps=6.6667, tpm_rps=None)}
        report = ExperimentReport(spec=spec, profiles=[ProfileReport(workload_name="short", recommendation=rec)])
        return build_capacity_profile(report)["workload_classes"]["short"]

    def test_inconclusive_observed_point_is_never_the_basis(self):
        """Observed non-failing 8 rps is INCONCLUSIVE; 4 rps is confirmed.
        The recommendation derives from 4, never 8."""
        inconclusive = Verdict("INCONCLUSIVE", [Check("throttle_rate", "INCONCLUSIVE", n=450, required_n=2995)])
        rec = Recommendation(point=SweepPoint(None, 8.0, _metrics()), saturation_point=None, verdict=inconclusive,
                             confirmed_point=SweepPoint(None, 4.0, _metrics()))
        entry = self._profile("rate", rec)
        self.assertEqual(entry["rate"]["observed_verdict"], "INCONCLUSIVE")
        env = entry["recommendation"]["admission_envelope"]
        self.assertEqual(env["basis"]["statistically_confirmed_offered_rps"], 4.0)
        self.assertEqual(env["sustained_rps"], 3.2)

    def test_inconclusive_with_nothing_confirmed_recommends_nothing(self):
        inconclusive = Verdict("INCONCLUSIVE", [Check("throttle_rate", "INCONCLUSIVE", n=450, required_n=2995)])
        rec = Recommendation(point=SweepPoint(4, None, _metrics()), saturation_point=None, verdict=inconclusive)
        entry = self._profile("concurrency", rec)
        self.assertEqual(entry["concurrency"]["observed_nonfailing"], 4)       # measurement still reported
        self.assertIsNone(entry["concurrency"]["statistically_confirmed"])
        self.assertIsNone(entry["recommendation"]["admission_envelope"])

    def test_measurement_and_recommendation_are_separate_blocks(self):
        rec = Recommendation(point=SweepPoint(None, 5.0, _metrics()), saturation_point=None,
                             confirmed_point=SweepPoint(None, 5.0, _metrics()))
        entry = self._profile("rate", rec)
        for policy_key in ("production_sustained_rps", "production_binding", "production_note", "headroom"):
            self.assertNotIn(policy_key, entry["rate"])
        # Existing confirmed measurement fields are unchanged.
        self.assertEqual(entry["rate"]["statistically_confirmed_offered_rps"], 5.0)
        self.assertEqual(entry["rate"]["observed_nonfailing_offered_rps"], 5.0)
        self.assertEqual(entry["recommendation"]["admission_envelope"]["sustained_rps"], 4.0)

    def test_no_sweep_value_passed_still_emits_a_null_recommendation(self):
        entry = self._profile("concurrency", None)
        self.assertIsNone(entry["recommendation"]["admission_envelope"])


if __name__ == "__main__":
    unittest.main()
