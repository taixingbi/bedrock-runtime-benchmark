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
  - {name: a, model_id: m.a-v1:0}
  - {name: b, model_id: m.b-v1:0, enabled: false}
  - {name: c, model_id: m.c-v1:0, region: us-west-2}
"""
QUOTAS = """
accounts:
  "111111111111":
    us-east-1:
      a: {rpm: 100, tpm: 1000, output_burndown: 5}
    us-west-2:
      c: {rpm: 50}
"""


class LoadModelsTests(unittest.TestCase):
    def setUp(self):
        self.path = _write(FILE)
        self.quota = _write(QUOTAS)

    def tearDown(self):
        Path(self.path).unlink()
        Path(self.quota).unlink()

    def _load(self, **kwargs):
        return load_models(self.path, quota_file=self.quota, **kwargs)

    def test_enabled_models_in_file_order_with_quota_joined_by_account_region_name(self):
        models = self._load()
        self.assertEqual([m.name for m in models], ["a", "c"])
        self.assertEqual((models[0].quota_rpm, models[0].quota_tpm, models[0].output_burndown), (100, 1000, 5))
        self.assertEqual((models[1].quota_rpm, models[1].quota_tpm, models[1].output_burndown), (50, None, 1.0))
        self.assertEqual({m.account for m in models}, {"111111111111"})

    def test_quota_is_region_scoped(self):
        """c's quota is filed under us-west-2; moving c to us-east-1
        must NOT pick it up."""
        path = _write(FILE.replace("region: us-west-2", "region: us-east-1"))
        try:
            c = load_models(path, quota_file=self.quota, names=["c"])[0]
            self.assertIsNone(c.quota_rpm)
        finally:
            Path(path).unlink()

    def test_model_without_a_quota_entry_has_no_quota(self):
        self.assertIsNone(self._load(names=["b"])[0].quota_rpm)

    def test_names_select_models_even_if_disabled(self):
        self.assertEqual([m.name for m in self._load(names=["b"])], ["b"])

    def test_include_disabled(self):
        self.assertEqual(len(self._load(include_disabled=True)), 3)

    def test_unknown_name_is_an_error(self):
        with self.assertRaisesRegex(ValueError, "unknown model"):
            self._load(names=["zzz"])

    def test_quota_in_the_models_file_is_rejected(self):
        path = _write("models: [{name: a, model_id: x, quota: {rpm: 1}}]")
        try:
            with self.assertRaisesRegex(ValueError, "belong in"):
                load_models(path, quota_file=self.quota)
        finally:
            Path(path).unlink()

    def test_quota_for_an_unknown_model_is_a_typo_error(self):
        quota = _write('accounts: {"111111111111": {us-east-1: {a: {rpm: 1}, nova-mikro: {rpm: 400}}}}')
        try:
            with self.assertRaisesRegex(ValueError, "nova-mikro"):
                load_models(self.path, quota_file=quota)
        finally:
            Path(quota).unlink()

    def test_names_must_be_folder_safe_and_unique(self):
        for text in [
            "models: [{name: 'us.amazon.nova:0', model_id: x}]",
            "models: [{name: Nova, model_id: x}]",
            "models: [{name: a, model_id: x}, {name: a, model_id: y}]",
        ]:
            path = _write(text)
            try:
                with self.subTest(text=text), self.assertRaises(ValueError):
                    load_models(path, quota_file=self.quota)
            finally:
                Path(path).unlink()


class AccountResolutionTests(unittest.TestCase):
    TWO = """
accounts:
  "111111111111":
    us-east-1: {a: {rpm: 100}}
  "222222222222":
    us-east-1: {a: {rpm: 999}}
"""

    def setUp(self):
        self.path = _write("models: [{name: a, model_id: m.a-v1:0}]")
        self.quota = _write(self.TWO)

    def tearDown(self):
        Path(self.path).unlink()
        Path(self.quota).unlink()

    def test_live_account_selects_its_own_quota(self):
        self.assertEqual(load_models(self.path, quota_file=self.quota, account="222222222222")[0].quota_rpm, 999)

    def test_several_accounts_and_no_live_account_is_an_error(self):
        with self.assertRaisesRegex(ValueError, "several accounts"):
            load_models(self.path, quota_file=self.quota)

    def test_live_account_missing_from_the_file_is_an_error(self):
        with self.assertRaisesRegex(ValueError, "333333333333"):
            load_models(self.path, quota_file=self.quota, account="333333333333")


class ShippedModelsTests(unittest.TestCase):
    def test_shipped_models_file_lists_the_five_certified_models_with_quotas(self):
        models = load_models(include_disabled=True, account="646821141010")
        self.assertEqual(
            {m.model_id for m in models},
            {"us.amazon.nova-micro-v1:0", "us.amazon.nova-lite-v1:0", "us.amazon.nova-pro-v1:0",
             "us.meta.llama3-3-70b-instruct-v1:0", "qwen.qwen3-32b-v1:0"},
        )
        self.assertTrue(all(m.quota_rpm and m.quota_tpm for m in models))


if __name__ == "__main__":
    unittest.main()
