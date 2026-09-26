import math
import random
import unittest

from bedrock_benchmark.analysis.capacity import FAIL, INCONCLUSIVE, PASS, SweepPoint, Verdict
from bedrock_benchmark.analysis.confirmation import (
    ConfirmationResult, RateLimit, fixed_sequence_confirmed, limits_for, plan_looks, reachable, required_samples,
    step,
)
from bedrock_benchmark.analysis.metrics import min_samples_to_resolve_rate, rate_lower, rate_upper, wilson_upper

GOLD = [RateLimit("throttle_rate", 0.001), RateLimit("success_rate", 0.005)]


def _plan(max_looks=2, **caps):
    kw = dict(max_repetitions=10, max_requests=8000, max_duration_s=1800)
    kw.update(caps)
    return plan_looks(GOLD, confidence=0.95, max_looks=max_looks, **kw)


class ExactBoundTests(unittest.TestCase):
    def test_clopper_pearson_bound_has_exact_coverage(self):
        """At the bound, P(X <= k) equals alpha -- checked against the
        binomial CDF directly."""
        for k, n in [(0, 2995), (1, 4742), (2, 6294), (7, 1000)]:
            p = rate_upper(k, n)
            cdf = sum(math.comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(k + 1))
            self.assertAlmostEqual(cdf, 0.05, places=4)

    def test_wilson_is_anti_conservative_at_zero_events(self):
        """Why the bound changed: Wilson clears 0.1% after 2,703 clean
        requests, but a true 0.1% rate yields 0 events there 6.7% of the
        time (> 5%). The exact bound needs 2,995 (0.999^2995 = 0.050)."""
        self.assertLessEqual(wilson_upper(0, 2703), 0.001)
        self.assertGreater(0.999 ** 2703, 0.06)
        self.assertEqual(min_samples_to_resolve_rate(0.001), 2995)
        self.assertLessEqual(0.999 ** 2995, 0.05)

    def test_lower_is_complement_of_upper(self):
        self.assertAlmostEqual(rate_lower(995, 1000), 1 - rate_upper(5, 1000))


class PlanTests(unittest.TestCase):
    def test_required_samples_grow_with_observed_events(self):
        self.assertEqual(required_samples(0, 0.001, confidence=0.95), 2995)
        self.assertEqual(required_samples(1, 0.001, confidence=0.95), 4742)
        self.assertEqual(required_samples(2, 0.001, confidence=0.95), 6294)

    def test_looks_are_bonferroni_corrected_and_fixed_up_front(self):
        one, two = _plan(max_looks=1), _plan(max_looks=2)
        self.assertEqual((one.per_look_confidence, one.look_schedule), (0.95, [2995]))
        self.assertAlmostEqual(two.per_look_confidence, 0.975)
        self.assertEqual(two.look_schedule, [3688, 5570])

    def test_mixed_class_limits_scale_by_share(self):
        limits = [RateLimit("throttle_rate", 0.01), RateLimit("gen.throttle_rate", 0.01, share=0.1)]
        plan = plan_looks(limits, confidence=0.95, max_looks=1, max_repetitions=10, max_requests=10**6,
                          max_duration_s=1e9)
        self.assertEqual(plan.look_schedule, [math.ceil(required_samples(0, 0.01, confidence=0.95) / 0.1)])

    def test_limits_for_blend_and_classes(self):
        gate = dict(throttle_rate_max=0.001, success_rate_min=0.995)
        names = [l.name for l in limits_for(gate, {"chat": dict(throttle_rate_max=0.01, success_rate_min=0.99)},
                                             {"chat": 0.6})]
        self.assertEqual(names, ["throttle_rate", "success_rate", "chat.throttle_rate", "chat.success_rate"])


class StepTests(unittest.TestCase):
    def test_pass_before_the_first_look_is_not_accepted(self):
        """The optional-stopping guard: a clean verdict at n < N1 keeps
        measuring instead of stopping on a lucky early bound."""
        self.assertIsNone(step(Verdict(PASS), 1000, 0, _plan()))

    def test_pass_at_a_scheduled_look_confirms(self):
        self.assertEqual(step(Verdict(PASS), 3700, 0, _plan()), (PASS, "confirmed", 1))

    def test_inconclusive_at_a_look_spends_it_and_continues(self):
        self.assertIsNone(step(Verdict(INCONCLUSIVE), 3700, 0, _plan()))
        self.assertEqual(step(Verdict(INCONCLUSIVE), 5600, 1, _plan()), (INCONCLUSIVE, "looks_exhausted", 2))

    def test_fail_stops_at_any_time(self):
        self.assertEqual(step(Verdict(FAIL), 100, 0, _plan()), (FAIL, "observed_violation", 0))

    def test_reachable_respects_every_cap(self):
        plan = _plan()  # first look at 3,688
        ok = dict(est_requests_per_rep=600, remaining_duration_s=1800, per_rep_s=100)
        self.assertTrue(reachable(plan, **ok))
        self.assertFalse(reachable(plan, **{**ok, "est_requests_per_rep": 300}))  # 10 reps x 300 < 3,688
        self.assertFalse(reachable(plan, **{**ok, "remaining_duration_s": 500}))  # 5 reps x 600 < 3,688
        self.assertFalse(reachable(_plan(max_requests=3000), **ok))


class FixedSequenceTests(unittest.TestCase):
    def test_highest_of_the_leading_pass_run(self):
        r = lambda v, verdict: ConfirmationResult(v, verdict, "x")
        self.assertEqual(fixed_sequence_confirmed([r(1, PASS), r(2, PASS), r(3, INCONCLUSIVE)]).value, 2)
        self.assertIsNone(fixed_sequence_confirmed([r(1, INCONCLUSIVE), r(2, PASS)]))


def _poisson(rng, lam):
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


class ErrorControlSimulationTests(unittest.TestCase):
    """At a true throttle rate exactly at gold's limit (the worst case
    for a false PASS), the planned procedure keeps false PASSes <= 5%;
    naive peeking after every repetition does not."""

    LIMIT, PER_REP, TRIALS = 0.001, 600, 2000

    def _verdict(self, k, n, conf):
        if k / n > self.LIMIT:
            return Verdict(FAIL)
        return Verdict(PASS if rate_upper(k, n, confidence=conf) <= self.LIMIT else INCONCLUSIVE)

    def _false_pass_rate(self, run) -> float:
        rng = random.Random(0)
        return sum(run(rng) == PASS for _ in range(self.TRIALS)) / self.TRIALS

    def test_planned_looks_control_false_pass(self):
        plan = plan_looks([RateLimit("t", self.LIMIT)], confidence=0.95, max_looks=2, max_repetitions=10,
                          max_requests=8000, max_duration_s=1e9)

        def planned(rng):
            k = n = looks = 0
            for _ in range(10):
                n += self.PER_REP
                k += _poisson(rng, self.PER_REP * self.LIMIT)
                d = step(self._verdict(k, n, plan.per_look_confidence), n, looks, plan)
                if d:
                    return d[0]
            return INCONCLUSIVE

        def naive(rng):
            k = n = 0
            for _ in range(10):
                n += self.PER_REP
                k += _poisson(rng, self.PER_REP * self.LIMIT)
                v = self._verdict(k, n, 0.95).verdict
                if v != INCONCLUSIVE:
                    return v
            return INCONCLUSIVE

        planned_rate, naive_rate = self._false_pass_rate(planned), self._false_pass_rate(naive)
        self.assertLessEqual(planned_rate, 0.05)
        self.assertGreater(naive_rate, 0.05)  # why the look schedule exists


if __name__ == "__main__":
    unittest.main()
