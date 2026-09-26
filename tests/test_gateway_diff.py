import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

from bedrock_benchmark.gateway_diff import diff

MODEL = "us.amazon.nova-micro-v1:0"


def _profile(**overrides) -> dict:
    profile = {
        "schema_version": 3,
        "model": {"provider": "bedrock", "model_id": MODEL, "region": "us-east-1"},
        "measurement": {"gate": "point_estimate", "min_requests_to_resolve_throttle_slo": 2703},
        "workload_classes": {
            "short": {
                "rate": {"max_safe_offered_rps": 6.0, "slo_goodput_rps": 5.8,
                         "saturation_offered_rps": 7.0, "production_offered_rps": 4.8},
                "evidence": {"n": 5000, "throttle_rate_upper": 0.0006},
                "workload_validation": {"valid": True},
            },
            "long_long": {
                "rate": {"max_safe_offered_rps": 3.0, "slo_goodput_rps": 2.9,
                         "saturation_offered_rps": 4.0, "production_offered_rps": 2.4},
                "evidence": {"n": 5000},
                "workload_validation": {"valid": True},
            },
        },
    }
    profile.update(overrides)
    return profile


def _kinds(result, severity=None):
    return {f.kind for f in result.findings if severity is None or f.severity == severity}


class GatewayDiffTests(unittest.TestCase):
    def test_model_rpm_above_the_tightest_class_envelope_is_proposed_down(self):
        result = diff([_profile()], {"models": {MODEL: {"rpm_limit": 400}}})

        f = next(f for f in result.findings if f.kind == "model_rpm_above_envelope")
        self.assertEqual((f.current, f.proposed), (400, 144))  # long_long binds: 2.4 rps * 60
        self.assertIn("long_long", f.basis)

    def test_model_rpm_within_envelope_is_info_only(self):
        result = diff([_profile()], {"models": {MODEL: {"rpm_limit": 100}}})
        self.assertEqual(_kinds(result, "warn"), set())
        self.assertIn("model_rpm_within_envelope", _kinds(result))

    def test_missing_model_rpm_is_flagged_as_fail_open(self):
        result = diff([_profile()], {})
        self.assertIn("model_rpm_unset", _kinds(result, "warn"))

    def test_mixed_envelope_counts_toward_the_binding_limit(self):
        profile = _profile(mixed_workloads={"blend": {"rate": {"production_offered_rps": 1.5}}})
        result = diff([profile], {"models": {MODEL: {"rpm_limit": 400}}})
        f = next(f for f in result.findings if f.kind == "model_rpm_above_envelope")
        self.assertEqual(f.proposed, 90)
        self.assertIn("mix blend", f.basis)

    def test_tenants(self):
        gateway = {
            "models": {MODEL: {"rpm_limit": 100}},
            "tenants": {
                "big": {"rpm_limit": 300, "models": [MODEL]},
                "small": {"rpm_limit": 50, "models": [MODEL]},
                "elsewhere": {"rpm_limit": 999, "models": ["other"]},
            },
        }
        result = diff([_profile()], gateway)
        above = [f for f in result.findings if f.kind == "tenant_rpm_above_envelope"]
        self.assertEqual([f.current for f in above], [300])
        over = next(f for f in result.findings if f.kind == "tenant_rpm_oversubscribed")
        self.assertEqual(over.current, 350)  # "elsewhere" doesn't use this model

    def test_concurrency_knobs(self):
        profile = _profile(workload_classes={"short": {"concurrency": {"production_max": 4}}})
        result = diff([profile], {"concurrency": {"global_max": 32, "default_tenant_max": 8, "processes": 2}})

        tenant = next(f for f in result.findings if f.kind == "tenant_concurrency_above_model_envelope")
        self.assertEqual((tenant.current, tenant.proposed), (8, 4))
        glob = next(f for f in result.findings if f.kind == "global_concurrency_exceeds_single_model_envelope")
        self.assertEqual((glob.severity, glob.current, glob.proposed), ("info", 64, None))

    def test_quality_findings(self):
        profile = _profile()
        profile["workload_classes"]["short"]["workload_validation"] = {
            "valid": False, "observed_input_tokens_p50": 700, "requested_input_tokens": 512, "deviation_pct": 36.7,
        }
        profile["workload_classes"]["long_long"]["evidence"] = {"n": 540, "throttle_rate_upper": 0.005}
        result = diff([profile], {"models": {MODEL: {"rpm_limit": 100}}})
        self.assertIn("workload_shape_invalid", _kinds(result, "warn"))
        self.assertIn("throttle_slo_unresolved", _kinds(result, "info"))

    def test_four_decimal_rps_rounding_does_not_propose_a_one_rpm_cut(self):
        """8.3333 rps x 0.8 headroom = 6.66664 rps = 399.998 rpm -- that's
        the 400 RPM quota, not a reason to propose 399."""
        profile = _profile(workload_classes={"short": {"rate": {"production_offered_rps": 6.66664}}})
        result = diff([profile], {"models": {MODEL: {"rpm_limit": 400}}})
        self.assertNotIn("model_rpm_above_envelope", _kinds(result))

    def test_v6_sustained_rps_and_unconfirmed_envelope(self):
        profile = _profile(schema_version=6, workload_classes={"short_chat": {
            "rate": {"production_sustained_rps": 4.5, "production_offered_rps": 99.0, "verdict": "INCONCLUSIVE",
                     "confirmed_safe": 1.67,
                     "inconclusive_checks": [{"name": "throttle_rate", "n": 450, "required_n": 2703}]},
        }})
        result = diff([profile], {"models": {MODEL: {"rpm_limit": 400}}})
        above = next(f for f in result.findings if f.kind == "model_rpm_above_envelope")
        self.assertEqual(above.proposed, 270)  # 4.5 rps -- the sustained (quota-capped) value, not 99
        unconfirmed = next(f for f in result.findings if f.kind == "envelope_unconfirmed")
        self.assertIn("2703", unconfirmed.message)
        self.assertIn("1.67", unconfirmed.message)

    def test_rejects_pre_v3_profiles(self):
        with self.assertRaises(ValueError):
            diff([_profile(schema_version=2)], {})

    def test_warn_findings_sort_first(self):
        result = diff([_profile()], {"models": {MODEL: {"rpm_limit": 400}}})
        self.assertEqual(result.findings[0].severity, "warn")


class GatewayDiffCliTests(unittest.TestCase):
    def test_cli_exits_1_on_warn_and_prints_yaml(self):
        with tempfile.TemporaryDirectory() as d:
            prof, gw = Path(d, "p.yaml"), Path(d, "gw.yaml")
            prof.write_text(yaml.safe_dump(_profile()))
            gw.write_text(yaml.safe_dump({"models": {MODEL: {"rpm_limit": 400}}}))
            proc = subprocess.run(
                [sys.executable, "scripts/gateway_diff.py", "--gateway-config", str(gw), str(prof)],
                capture_output=True, text=True,
            )
        self.assertEqual(proc.returncode, 1, proc.stderr)
        out = yaml.safe_load(proc.stdout)
        self.assertEqual(out["summary"]["proposed_changes"], 1)

    def test_example_gateway_config_parses(self):
        gw = yaml.safe_load(Path("examples/gateway-limits.example.yaml").read_text())
        diff([_profile()], gw)


if __name__ == "__main__":
    unittest.main()
