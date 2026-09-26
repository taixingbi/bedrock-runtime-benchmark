import random
import unittest

from bedrock_benchmark.workload import WorkloadMix, WorkloadProfile


class WorkloadProfileTests(unittest.TestCase):
    def test_prompt_is_roughly_the_target_input_length(self):
        profile = WorkloadProfile(name="short", input_tokens=512, output_tokens=64)
        prompt = profile.prompt()
        estimated_tokens = len(prompt) / 4
        # Within a generous band -- this is a heuristic filler generator,
        # not a real tokenizer, so exactness isn't the point (see the
        # module's own docstring: the REAL input_tokens comes back from
        # Bedrock's response, not this estimate).
        self.assertGreater(estimated_tokens, 400)
        self.assertLess(estimated_tokens, 700)

    def test_prompt_asks_for_more_than_the_output_budget(self):
        """Asking for "about N words" let models stop early (end_turn at
        42-50% of the target on nova-micro). The prompt must ask for MORE
        words than the token budget, so generation ends on max_tokens."""
        def asked(w):
            return int(w.prompt().split("at least ")[1].split(" words")[0])

        short = WorkloadProfile(name="s", input_tokens=100, output_tokens=64)
        long = WorkloadProfile(name="l", input_tokens=100, output_tokens=1024)
        self.assertGreater(asked(short), short.output_tokens)
        self.assertGreater(asked(long), long.output_tokens)
        self.assertIn("do not stop early", long.prompt())

    def test_different_profiles_produce_different_prompts(self):
        a = WorkloadProfile(name="a", input_tokens=100, output_tokens=64)
        b = WorkloadProfile(name="b", input_tokens=4096, output_tokens=64)
        self.assertNotEqual(a.prompt(), b.prompt())


class WorkloadMixTests(unittest.TestCase):
    def test_samples_classes_in_proportion_to_weight(self):
        short = WorkloadProfile(name="short", input_tokens=512, output_tokens=64)
        long = WorkloadProfile(name="long", input_tokens=4096, output_tokens=512)
        mix = WorkloadMix(name="m", entries=[(short, 7), (long, 3)])
        rng = random.Random(0)

        draws = [mix.sample(rng).name for _ in range(5000)]

        self.assertAlmostEqual(draws.count("short") / 5000, 0.7, delta=0.03)
        self.assertEqual(mix.shares, {"short": 0.7, "long": 0.3})

    def test_rejects_empty_or_nonpositive_weights(self):
        p = WorkloadProfile(name="p", input_tokens=1, output_tokens=1)
        with self.assertRaises(ValueError):
            WorkloadMix(name="m", entries=[])
        with self.assertRaises(ValueError):
            WorkloadMix(name="m", entries=[(p, 0)])


if __name__ == "__main__":
    unittest.main()
