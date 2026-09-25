import unittest

from bedrock_benchmark.workload import WorkloadProfile


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

    def test_prompt_requests_output_of_roughly_the_target_length(self):
        short = WorkloadProfile(name="s", input_tokens=100, output_tokens=64)
        long = WorkloadProfile(name="l", input_tokens=100, output_tokens=512)
        # The long-output profile's prompt should ask for more words.
        short_words = int(short.prompt().split("approximately ")[1].split(" words")[0])
        long_words = int(long.prompt().split("approximately ")[1].split(" words")[0])
        self.assertGreater(long_words, short_words)

    def test_different_profiles_produce_different_prompts(self):
        a = WorkloadProfile(name="a", input_tokens=100, output_tokens=64)
        b = WorkloadProfile(name="b", input_tokens=4096, output_tokens=64)
        self.assertNotEqual(a.prompt(), b.prompt())


if __name__ == "__main__":
    unittest.main()
