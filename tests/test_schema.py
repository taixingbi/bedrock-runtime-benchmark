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
        self.assertEqual(spec.quota_snapshot.rpm, 400)
        self.assertEqual(spec.quota_snapshot.tpm, 8000000)
        self.assertEqual(spec.slo.ttft_p95_ms, 1000)
        self.assertEqual(spec.slo.success_rate_min, 0.99)

    def test_token_sweep_has_four_workload_shapes(self):
        spec = load_experiment("experiments/token-sweep.yaml")
        self.assertEqual(len(spec.workloads), 4)

    def test_slo_capacity_is_a_rate_sweep(self):
        spec = load_experiment("experiments/slo-capacity.yaml")
        self.assertEqual(spec.sweep.type, "rate")

    def test_all_three_have_explicit_transport_config(self):
        for name in ["concurrency-sweep", "token-sweep", "slo-capacity"]:
            with self.subTest(name=name):
                spec = load_experiment(f"experiments/{name}.yaml")
                self.assertEqual(spec.transport.total_max_attempts, 1)
                self.assertEqual(spec.transport.max_connections, 64)

    def test_transport_defaults_when_not_specified_in_yaml(self):
        """A minimal experiment YAML with no transport: block at all
        must still get safe, explicit defaults (not boto3's own
        implicit ones) -- see client.py's TransportConfig docstring."""
        import tempfile
        from pathlib import Path

        minimal = """
        name: minimal
        target: {model_id: m, region: us-east-1}
        workloads: [{name: w, input_tokens: 100, output_tokens: 16}]
        sweep: {type: concurrency, values: [1]}
        """
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(minimal)
            path = f.name
        try:
            spec = load_experiment(path)
            self.assertEqual(spec.transport.total_max_attempts, 1)
        finally:
            Path(path).unlink()


    def test_measurement_fields_default_and_validate(self):
        import tempfile
        from pathlib import Path

        base = (
            "name: minimal\n"
            "target: {model_id: m, region: us-east-1}\n"
            "workloads: [{name: w, input_tokens: 100, output_tokens: 16}]\n"
            "sweep: {type: concurrency, values: [1]}\n"
        )
        cases = [
            (base, None),
            (base + "repetitions: 0\n", ValueError),
            (base + "slo: {confidence: 1.5}\n", ValueError),
            (base + "mix: {name: x, weights: {nope: 1}}\n", ValueError),
            (base + "mix: {name: x, weights: {w: 0}}\n", ValueError),
        ]
        for text, expected_error in cases:
            with self.subTest(text=text), tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
                f.write(text)
                path = f.name
            try:
                if expected_error:
                    with self.assertRaises(expected_error):
                        load_experiment(path)
                else:
                    spec = load_experiment(path)
                    self.assertEqual((spec.warmup_s, spec.repetitions, spec.slo.confidence), (0.0, 1, None))
            finally:
                Path(path).unlink()

    def test_every_shipped_experiment_loads(self):
        from pathlib import Path
        for path in sorted(Path("experiments").glob("*.yaml")):
            with self.subTest(path=path.name):
                load_experiment(str(path))

    def test_mixed_capacity_experiment_defines_a_valid_mix(self):
        spec = load_experiment("experiments/mixed-capacity.yaml")
        self.assertEqual(spec.mix.weights, {"short": 0.7, "long_long": 0.3})
        self.assertEqual(spec.workload_validation_tolerance_pct, 10.0)


if __name__ == "__main__":
    unittest.main()
