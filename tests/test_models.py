import tempfile
import unittest
from pathlib import Path

from bedrock_benchmark.models import load_models


def _write(text: str) -> str:
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    f.write(text)
    f.close()
    return f.name


FILE = """
models:
  - {name: a, model_id: m.a-v1:0, quota: {rpm: 100, tpm: 1000}}
  - {name: b, model_id: m.b-v1:0, enabled: false}
  - {name: c, model_id: m.c-v1:0, region: us-west-2}
"""


class LoadModelsTests(unittest.TestCase):
    def setUp(self):
        self.path = _write(FILE)

    def tearDown(self):
        Path(self.path).unlink()

    def test_enabled_models_in_file_order_with_quota(self):
        models = load_models(self.path)
        self.assertEqual([m.name for m in models], ["a", "c"])
        self.assertEqual((models[0].quota_rpm, models[0].quota_tpm), (100, 1000))
        self.assertEqual(models[1].region, "us-west-2")

    def test_names_select_models_even_if_disabled(self):
        self.assertEqual([m.name for m in load_models(self.path, names=["b"])], ["b"])

    def test_include_disabled(self):
        self.assertEqual(len(load_models(self.path, include_disabled=True)), 3)

    def test_unknown_name_is_an_error(self):
        with self.assertRaisesRegex(ValueError, "unknown model"):
            load_models(self.path, names=["zzz"])

    def test_names_must_be_folder_safe_and_unique(self):
        for text in [
            "models: [{name: 'us.amazon.nova:0', model_id: x}]",
            "models: [{name: Nova, model_id: x}]",
            "models: [{name: a, model_id: x}, {name: a, model_id: y}]",
        ]:
            path = _write(text)
            try:
                with self.subTest(text=text), self.assertRaises(ValueError):
                    load_models(path)
            finally:
                Path(path).unlink()

    def test_shipped_models_file_lists_the_five_certified_models_with_quotas(self):
        models = load_models(include_disabled=True)
        self.assertEqual(
            {m.model_id for m in models},
            {"us.amazon.nova-micro-v1:0", "us.amazon.nova-lite-v1:0", "us.amazon.nova-pro-v1:0",
             "us.meta.llama3-3-70b-instruct-v1:0", "qwen.qwen3-32b-v1:0"},
        )
        self.assertTrue(all(m.quota_rpm and m.quota_tpm for m in models))


if __name__ == "__main__":
    unittest.main()
