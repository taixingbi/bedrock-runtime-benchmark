import tempfile
import unittest
from pathlib import Path

import yaml

from bedrock_benchmark.drift import summarize


def _profile(measured_at, confirmed, production, schema=10):
    return {
        "schema_version": schema, "experiment": "rate-capacity", "model": {"name": "nova-micro"},
        **({"environment": {"measured_at": {"start": measured_at}, "git_commit": "abc"}} if measured_at else {}),
        "workload_classes": {"short_chat": {"rate": {
            "statistically_confirmed_offered_rps": confirmed, "production_sustained_rps": production}}},
    }


class DriftTests(unittest.TestCase):
    def _write(self, d, name, profile):
        path = Path(d) / name / "nova-micro" / "rate-capacity-x-capacity-profile.yaml"
        path.parent.mkdir(parents=True)
        path.write_text(yaml.safe_dump(profile))

    def test_repeated_runs_over_days(self):
        with tempfile.TemporaryDirectory() as d:
            self._write(d, "a", _profile("2026-09-26T10:00:00+00:00", 5.0, 4.0))
            self._write(d, "b", _profile("2026-09-27T22:00:00+00:00", 4.2, 3.36))
            self._write(d, "c", _profile("2026-09-28T03:00:00+00:00", 5.8, 4.64))
            [g] = summarize([d])
        self.assertEqual((g["repeated_runs"], g["days_observed"], g["confirmed_runs"]), (3, 3, 3))
        self.assertEqual(g["confirmed"], {"min": 4.2, "median": 5.0, "max": 5.8, "spread_pct": 32.0})
        self.assertFalse(g["stable"])                      # 32% spread > 20%
        self.assertEqual(g["conservative_production"], 3.36)
        self.assertEqual([r["confirmed"] for r in g["runs"]], [5.0, 4.2, 5.8])  # oldest first

    def test_unconfirmed_runs_and_single_day(self):
        with tempfile.TemporaryDirectory() as d:
            self._write(d, "a", _profile("2026-09-26T10:00:00+00:00", 5.0, 4.0))
            self._write(d, "b", _profile("2026-09-26T15:00:00+00:00", None, None))
            [g] = summarize([d])
        self.assertEqual((g["repeated_runs"], g["days_observed"], g["confirmed_runs"]), (2, 1, 1))
        self.assertFalse(g["stable"])                      # one confirmed value is no evidence of stability
        self.assertIn("fewer than 2", g["note"])

    def test_old_profiles_without_environment_still_count_as_runs(self):
        with tempfile.TemporaryDirectory() as d:
            self._write(d, "a", _profile(None, 5.0, 4.0, schema=8))
            [g] = summarize([d])
        self.assertEqual((g["repeated_runs"], g["days_observed"]), (1, 0))


if __name__ == "__main__":
    unittest.main()
