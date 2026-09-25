import unittest

from bedrock_benchmark.experiments.schema import load_experiment


class LoadExperimentTests(unittest.TestCase):
    def test_loads_the_three_shipped_experiments_without_error(self):
        for name in ["concurrency-sweep", "token-sweep", "slo-capacity"]:
            with self.subTest(name=name):
                spec = load_experiment(f"experiments/{name}.yaml")
                self.assertTrue(spec.workloads)
                self.assertIn(spec.sweep.type, ("concurrency", "rate"))

    def test_concurrency_sweep_has_real_quota_and_slo(self):
        spec = load_experiment("experiments/concurrency-sweep.yaml")
        self.assertEqual(spec.quota.rpm, 400)
        self.assertEqual(spec.quota.tpm, 8000000)
        self.assertEqual(spec.slo.ttft_p95_ms, 1000)

    def test_token_sweep_has_four_workload_shapes(self):
        spec = load_experiment("experiments/token-sweep.yaml")
        self.assertEqual(len(spec.workloads), 4)

    def test_slo_capacity_is_a_rate_sweep(self):
        spec = load_experiment("experiments/slo-capacity.yaml")
        self.assertEqual(spec.sweep.type, "rate")


if __name__ == "__main__":
    unittest.main()
