import copy
import tempfile
import unittest
from pathlib import Path

import yaml

from bedrock_benchmark.drift import summarize


def _profile(measured_at, confirmed, production, schema=10):
    return {
        "schema_version": schema, "experiment": "capacity-reference-rate", "model": {"name": "nova-micro"},
        **({"environment": {"measured_at": {"start": measured_at}, "git_commit": "abc"}} if measured_at else {}),
        "workload_classes": {"short_chat": {"rate": {
            "statistically_confirmed_offered_rps": confirmed, "production_sustained_rps": production}}},
    }


class DriftTests(unittest.TestCase):
    def _write(self, d, name, profile):
        path = Path(d) / name / "nova-micro" / "capacity-reference-rate-x-capacity-profile.yaml"
        path.parent.mkdir(parents=True)
        path.write_text(yaml.safe_dump(profile))

    def test_comparison_groups_separate_modes_candidates_and_exposure(self):
        base = _profile("2026-09-26T10:00:00+00:00", 5.0, 4.0)
        base["mode"] = "sustain"
        base["measurement"] = {
            "window_s": 1800,
            "confirmation": {"min_steady_state_duration_s": 1800},
            "retest": {"workload": "short_chat", "concurrency": 7, "duration_s": 1800},
        }
        variants = [copy.deepcopy(base) for _ in range(6)]
        variants[0]["mode"] = "sweep"
        variants[1]["measurement"]["retest"]["concurrency"] = 6
        variants[2]["measurement"]["window_s"] = 3600
        variants[3]["measurement"]["confirmation"]["min_steady_state_duration_s"] = 3600
        variants[4]["measurement"]["retest"]["duration_s"] = 3600
        variants[5]["measurement"]["retest"]["workload"] = "another_class"
        with tempfile.TemporaryDirectory() as d:
            self._write(d, "base", base)
            self._write(d, "same", base)
            for i, variant in enumerate(variants):
                self._write(d, str(i), variant)
            groups = summarize([d])
        self.assertEqual(len(groups), 7)
        self.assertEqual(sorted(g["temporal_validation"]["runs"] for g in groups),
                         [1, 1, 1, 1, 1, 1, 2])

    def test_historical_names_and_sustain_mode_are_preserved(self):
        current = _profile(None, 5.0, 4.0)
        legacy = copy.deepcopy(current)
        legacy["experiment"] = "rate-capacity"
        sustain = copy.deepcopy(current)
        sustain["experiment"] = "workload-shape-calibration-sustain"
        with tempfile.TemporaryDirectory() as d:
            for i, profile in enumerate([current, legacy, sustain]):
                self._write(d, str(i), profile)
            groups = summarize([d])
        self.assertEqual(len(groups), 3)
        modes = {g["experiment"]: g["mode"] for g in groups}
        self.assertEqual(modes["workload-shape-calibration-sustain"], "sustain")
        self.assertEqual(modes["rate-capacity"], "sweep")

    def test_repeated_runs_over_days_and_times_are_an_unstable_envelope(self):
        with tempfile.TemporaryDirectory() as d:
            self._write(d, "a", _profile("2026-09-26T10:00:00+00:00", 5.0, 4.0))
            self._write(d, "b", _profile("2026-09-27T22:00:00+00:00", 4.2, 3.36))
            self._write(d, "c", _profile("2026-09-28T03:00:00+00:00", 5.8, 4.64))
            [g] = summarize([d])
        tv = g["temporal_validation"]
        self.assertEqual((tv["runs"], tv["days_observed"], tv["confirmed_runs"]), (3, 3, 3))
        self.assertEqual(tv["utc_hours_observed"], [3, 10, 22])
        self.assertEqual(tv["confirmed_rps"], {"min": 4.2, "median": 5.0, "max": 5.8, "spread_pct": 32.0})
        self.assertEqual(tv["conservative_rps"], 4.2)
        self.assertEqual(tv["conservative_admission"], {"sustained_rps": 3.36})
        self.assertEqual(tv["envelope"], "unstable_operating_envelope")  # 32% spread > 20%
        self.assertEqual(tv["production_capacity_input"], {"sustained_rps": 3.36})  # the conservative value
        self.assertEqual([r["confirmed"] for r in g["runs"]], [5.0, 4.2, 5.8])  # oldest first

    def test_consistent_runs_are_a_stable_envelope(self):
        with tempfile.TemporaryDirectory() as d:
            for i, (t, v) in enumerate([("2026-09-26T08:00", 6.67), ("2026-09-26T20:00", 6.67),
                                        ("2026-09-27T09:00", 6.0)]):
                self._write(d, str(i), _profile(t + ":00+00:00", v, round(v * 0.8, 4)))
            [g] = summarize([d])
        self.assertEqual(g["temporal_validation"]["envelope"], "stable_operating_envelope")  # 10% spread

    def test_a_run_that_confirmed_nothing_is_never_stable(self):
        with tempfile.TemporaryDirectory() as d:
            self._write(d, "a", _profile("2026-09-26T10:00:00+00:00", 5.0, 4.0))
            self._write(d, "b", _profile("2026-09-26T15:00:00+00:00", 5.0, 4.0))
            self._write(d, "c", _profile("2026-09-27T15:00:00+00:00", None, None))
            [g] = summarize([d])
        tv = g["temporal_validation"]
        self.assertEqual((tv["confirmed_runs"], tv["unconfirmed_runs"]), (2, 1))
        self.assertEqual(tv["envelope"], "unstable_operating_envelope")

    def test_invalid_runs_are_listed_but_never_evidence(self):
        with tempfile.TemporaryDirectory() as d:
            for i, (t, v) in enumerate([("2026-09-26T08:00", 6.67), ("2026-09-26T20:00", 6.67),
                                        ("2026-09-27T09:00", 6.0)]):
                self._write(d, str(i), _profile(t + ":00+00:00", v, round(v * 0.8, 4)))
            bad = _profile("2026-09-28T09:00:00+00:00", 1.0, 0.8)
            bad["workload_classes"]["short_chat"]["measurement_validity"] = {"status": "invalid"}
            self._write(d, "bad", bad)
            [g] = summarize([d])
        tv = g["temporal_validation"]
        self.assertEqual((tv["runs"], tv["invalid_runs"]), (3, 1))
        self.assertEqual(tv["conservative_rps"], 6.0)  # the invalid 1.0 is not the minimum
        self.assertEqual(len(g["runs"]), 4)            # still listed

    def test_too_few_runs_or_days_is_insufficient_evidence(self):
        with tempfile.TemporaryDirectory() as d:
            self._write(d, "a", _profile("2026-09-26T10:00:00+00:00", 5.0, 4.0))
            self._write(d, "b", _profile("2026-09-26T15:00:00+00:00", 5.0, 4.0))
            [g] = summarize([d])
        self.assertEqual(g["temporal_validation"]["envelope"], "insufficient_temporal_evidence")
        self.assertIsNone(g["temporal_validation"]["production_capacity_input"])

    def test_one_run_is_a_single_run_envelope(self):
        with tempfile.TemporaryDirectory() as d:
            self._write(d, "a", _profile(None, 5.0, 4.0, schema=8))  # no environment: counts, no days
            [g] = summarize([d])
        tv = g["temporal_validation"]
        self.assertEqual((tv["runs"], tv["days_observed"], tv["envelope"]), (1, 0, "single_run_operating_envelope"))
        self.assertIsNone(tv["production_capacity_input"])  # one run is never production input


if __name__ == "__main__":
    unittest.main()


def test_different_slo_policies_do_not_pool(tmp_path):
    a = _profile("2026-09-26T10:00:00+00:00", 5, 4)
    b = copy.deepcopy(a)
    a["constraints"] = {"slo":{"effective_by_workload":{"short_chat":{"ttft_p95_ms":800}}}}
    b["constraints"] = {"slo":{"effective_by_workload":{"short_chat":{"ttft_p95_ms":1500}}}}
    for name, profile in (("a",a),("b",b)):
        (tmp_path / f"{name}-capacity-profile.yaml").write_text(yaml.safe_dump(profile))
    assert len(summarize([str(tmp_path)])) == 2


def test_continuous_and_accumulated_exposure_do_not_pool(tmp_path):
    a = _profile('2026-09-26T10:00:00+00:00', 5, 4)
    a['measurement'] = {'confirmation': {'min_steady_state_duration_s': 300, 'continuous': False}}
    b = copy.deepcopy(a)
    b['measurement']['confirmation']['continuous'] = True
    for name, profile in [('a', a), ('b', b)]:
        (tmp_path / f'{name}-capacity-profile.yaml').write_text(yaml.safe_dump(profile))
    assert len(summarize([str(tmp_path)])) == 2
