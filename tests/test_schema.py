import tempfile
import unittest
from pathlib import Path

from bedrock_benchmark.experiments.schema import load_experiment
from bedrock_benchmark.models import ModelConfig, load_models

MICRO = ModelConfig(name="nova-micro", model_id="us.amazon.nova-micro-v1:0", quota_rpm=400, quota_tpm=8_000_000)
PRO = ModelConfig(name="nova-pro", model_id="us.amazon.nova-pro-v1:0", quota_rpm=50, quota_tpm=2_000_000)
NO_QUOTA = ModelConfig(name="mystery", model_id="x.y-v1:0")

MINIMAL = (
    "name: minimal\n"
    "workloads: [short_chat]\n"
    "sweep: {type: concurrency, values: [1]}\n"
)


def _load_text(text: str, model: ModelConfig = MICRO):
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(text)
        path = f.name
    try:
        return load_experiment(path, model)
    finally:
        Path(path).unlink()


class ShippedExperimentTests(unittest.TestCase):
    def test_every_shipped_experiment_binds_to_every_shipped_model(self):
        for path in sorted(Path("experiments").glob("*.yaml")):
            for model in load_models(include_disabled=True):
                with self.subTest(experiment=path.name, model=model.name):
                    spec = load_experiment(str(path), model)
                    self.assertEqual(spec.target.model_id, model.model_id)
                    self.assertEqual(spec.model_name, model.name)
                    self.assertEqual(spec.transport.total_max_attempts, 1)

    def test_experiment_files_never_name_a_model(self):
        model_words = {m.name for m in load_models(include_disabled=True)} | {"nova", "llama", "qwen"}
        for path in sorted(Path("experiments").glob("*.yaml")):
            spec = load_experiment(str(path), MICRO)
            with self.subTest(path=path.name):
                self.assertFalse(any(w in spec.name for w in model_words), spec.name)
                self.assertFalse(any(w in path.stem for w in model_words), path.stem)

    def test_token_sweep_covers_every_catalog_workload(self):
        names = [w.name for w in load_experiment("experiments/token-sweep.yaml", MICRO).workloads]
        self.assertEqual(names, ["short_chat", "rag_answer", "long_generation"])

    def test_mixed_capacity_defines_a_valid_mix(self):
        spec = load_experiment("experiments/mixed-capacity.yaml", MICRO)
        self.assertEqual(spec.mix.weights, {"short_chat": 0.6, "rag_answer": 0.3, "long_generation": 0.1})


class ModelBindingTests(unittest.TestCase):
    def test_target_and_quota_come_from_the_model(self):
        spec = load_experiment("experiments/concurrency-sweep.yaml", PRO)
        self.assertEqual((spec.target.model_id, spec.target.region), ("us.amazon.nova-pro-v1:0", "us-east-1"))
        self.assertEqual((spec.quota_snapshot.rpm, spec.quota_snapshot.tpm), (50, 2_000_000))

    def test_quota_fractions_resolve_against_each_models_own_quota(self):
        micro = load_experiment("experiments/rate-capacity.yaml", MICRO)
        pro = load_experiment("experiments/rate-capacity.yaml", PRO)
        self.assertEqual(micro.sweep.quota_fractions, pro.sweep.quota_fractions)
        i = micro.sweep.quota_fractions.index(1.0)
        self.assertAlmostEqual(micro.sweep_values("short_chat")[i], 400 / 60, places=3)  # 1.0x ceiling (RPM-bound)
        self.assertAlmostEqual(pro.sweep_values("short_chat")[i], 50 / 60, places=3)

    def test_quota_relative_sweep_without_a_quota_fails_clearly(self):
        with self.assertRaisesRegex(ValueError, "quota.rpm"):
            load_experiment("experiments/rate-capacity.yaml", NO_QUOTA)

    def test_concurrency_sweep_needs_no_quota(self):
        load_experiment("experiments/concurrency-sweep.yaml", NO_QUOTA)

    def test_experiment_files_with_a_model_are_rejected(self):
        for key in ("target: {model_id: m}\n", "quota_snapshot: {rpm: 1}\n"):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "model-agnostic"):
                _load_text(MINIMAL + key)


class SloProfileTests(unittest.TestCase):
    def test_workloads_resolve_the_slo_profile_the_catalog_binds(self):
        spec = load_experiment("experiments/token-sweep.yaml", MICRO)
        self.assertEqual(spec.slo_for("short_chat").latency_p95_ms, 3000)       # gold
        self.assertEqual(spec.slo_for("rag_answer").latency_p95_ms, 6000)       # silver
        self.assertEqual(spec.slo_for("long_generation").latency_p95_ms, 15000) # bronze


class ValidationTests(unittest.TestCase):
    def test_defaults(self):
        spec = _load_text(MINIMAL)
        self.assertEqual((spec.warmup_s, spec.repetitions, spec.slo.confidence), (0.0, 1, None))
        self.assertEqual(spec.transport.total_max_attempts, 1)
        self.assertEqual(spec.workload_validation_tolerance_pct, 10.0)

    def test_invalid_specs_are_rejected(self):
        bad = [
            MINIMAL + "repetitions: 0\n",
            MINIMAL + "slo: {confidence: 1.5}\n",
            MINIMAL + "mix: {name: x, weights: {nope: 1}}\n",
            MINIMAL + "mix: {name: x, weights: {short_chat: 0}}\n",
            MINIMAL.replace("values: [1]", "values: [1], quota_fractions: [1.0]"),
            MINIMAL.replace("values: [1]", "quota_fractions: [1.0]"),  # concurrency can't be quota-relative
            MINIMAL.replace("{type: concurrency, values: [1]}", "{type: rate}"),
            MINIMAL.replace("values: [1]", "values: [1, 128]"),  # > transport.max_connections (64)
            MINIMAL.replace("[short_chat]", "[nope]"),                          # not in the catalog
            MINIMAL.replace("[short_chat]", "[short_chat, short_chat]"),        # listed twice
            MINIMAL.replace("[short_chat]", "[{name: w, input_tokens: 1, output_tokens: 1}]"),  # inline shape
            MINIMAL + "mix: {name: x, weights: {rag_answer: 1}}\n",            # mixes an unlisted workload
            MINIMAL + "transport: {max_connections: 64, executor_workers: 8}\n",
        ]
        for text in bad:
            with self.subTest(text=text), self.assertRaises(ValueError):
                _load_text(text)


if __name__ == "__main__":
    unittest.main()
