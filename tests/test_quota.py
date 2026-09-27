import unittest
from unittest.mock import MagicMock

from bedrock_benchmark.quota import fetch_quota_snapshot


class _FakePaginator:
    def __init__(self, quotas):
        self._quotas = quotas

    def paginate(self, **_kwargs):
        yield {"Quotas": self._quotas}


def _sq_with_quotas(quotas):
    client = MagicMock()
    client.get_paginator.return_value = _FakePaginator(quotas)
    return client


class FetchQuotaSnapshotTests(unittest.TestCase):
    def test_service_quotas_is_the_source(self):
        sq = _sq_with_quotas([
            {"QuotaName": "Cross-region model inference requests per minute for Amazon Nova Micro", "Value": 400},
            {"QuotaName": "Cross-region model inference tokens per minute for Amazon Nova Micro", "Value": 8_000_000},
        ])

        quota = fetch_quota_snapshot("us.amazon.nova-micro-v1:0", sq_client=sq)

        self.assertEqual((quota.rpm, quota.tpm, quota.source), (400.0, 8_000_000.0, "service_quotas"))

    def test_service_quotas_error_is_unknown_not_a_crash(self):
        sq = MagicMock()
        sq.get_paginator.side_effect = Exception("AccessDenied")
        self.assertEqual(fetch_quota_snapshot("us.amazon.nova-micro-v1:0", sq_client=sq).source, "unknown")

    def test_neither_source_available_returns_unknown_not_a_crash(self):
        sq = _sq_with_quotas([])  # no matching quota name at all

        quota = fetch_quota_snapshot("us.amazon.nova-micro-v1:0", sq_client=sq)

        self.assertIsNone(quota.rpm)
        self.assertIsNone(quota.tpm)
        self.assertEqual(quota.source, "unknown")

    def test_unmapped_model_id_skips_service_quotas_and_returns_unknown(self):
        sq = MagicMock()

        quota = fetch_quota_snapshot("some-model-not-in-the-mapping", sq_client=sq)

        self.assertEqual(quota.source, "unknown")
        sq.get_paginator.assert_not_called()

    def test_on_demand_model_uses_on_demand_quota_name(self):
        sq = _sq_with_quotas([
            {"QuotaName": "On-demand model inference requests per minute for Qwen3 32B V1", "Value": 1000},
            {"QuotaName": "On-demand model inference tokens per minute for Qwen3 32B V1", "Value": 100_000_000},
        ])

        quota = fetch_quota_snapshot("qwen.qwen3-32b-v1:0", sq_client=sq)

        self.assertEqual(quota.rpm, 1000.0)
        self.assertEqual(quota.tpm, 100_000_000.0)



class ScopeTests(unittest.TestCase):
    def test_the_benchmark_reads_no_gateway_infrastructure(self):
        """The producer depends only on Bedrock / AWS, never on a
        consumer's tables or config."""
        from pathlib import Path
        for path in Path("src/bedrock_benchmark").rglob("*.py"):
            text = path.read_text().lower()
            with self.subTest(path=str(path)):
                self.assertNotIn("dynamodb", text)
                self.assertNotIn("gateway-model-quotas", text)


if __name__ == "__main__":
    unittest.main()
