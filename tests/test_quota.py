import unittest
from unittest.mock import MagicMock

from bedrock_benchmark.quota import fetch_quota_snapshot


def _dynamo_with_item(rpm=None, tpm=None):
    client = MagicMock()
    if rpm is None:
        client.get_item.return_value = {}
        return client
    item = {"rpm_limit": {"N": str(rpm)}}
    if tpm is not None:
        item["tpm_limit"] = {"N": str(tpm)}
    client.get_item.return_value = {"Item": item}
    return client


def _dynamo_raising():
    client = MagicMock()
    client.get_item.side_effect = Exception("ResourceNotFoundException")
    return client


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
    def test_table_hit_is_used_and_marked_as_source_table(self):
        dynamo = _dynamo_with_item(rpm=400, tpm=8_000_000)
        quota = fetch_quota_snapshot("us.amazon.nova-micro-v1:0", dynamo_client=dynamo, sq_client=MagicMock())

        self.assertEqual(quota.rpm, 400.0)
        self.assertEqual(quota.tpm, 8_000_000.0)
        self.assertEqual(quota.source, "table")

    def test_table_miss_falls_back_to_service_quotas(self):
        dynamo = _dynamo_with_item(rpm=None)  # empty response -- no Item key
        sq = _sq_with_quotas([
            {"QuotaName": "Cross-region model inference requests per minute for Amazon Nova Micro", "Value": 400},
            {"QuotaName": "Cross-region model inference tokens per minute for Amazon Nova Micro", "Value": 8_000_000},
        ])

        quota = fetch_quota_snapshot("us.amazon.nova-micro-v1:0", dynamo_client=dynamo, sq_client=sq)

        self.assertEqual(quota.rpm, 400.0)
        self.assertEqual(quota.tpm, 8_000_000.0)
        self.assertEqual(quota.source, "service_quotas")

    def test_table_error_also_falls_back_to_service_quotas(self):
        """Table missing/unreachable/no-permission must all fall
        through cleanly, not raise -- this is a soft convenience, not
        a hard dependency (see quota.py's own docstring)."""
        dynamo = _dynamo_raising()
        sq = _sq_with_quotas([
            {"QuotaName": "Cross-region model inference requests per minute for Amazon Nova Pro", "Value": 50},
        ])

        quota = fetch_quota_snapshot("us.amazon.nova-pro-v1:0", dynamo_client=dynamo, sq_client=sq)

        self.assertEqual(quota.rpm, 50.0)
        self.assertEqual(quota.source, "service_quotas")

    def test_neither_source_available_returns_unknown_not_a_crash(self):
        dynamo = _dynamo_with_item(rpm=None)
        sq = _sq_with_quotas([])  # no matching quota name at all

        quota = fetch_quota_snapshot("us.amazon.nova-micro-v1:0", dynamo_client=dynamo, sq_client=sq)

        self.assertIsNone(quota.rpm)
        self.assertIsNone(quota.tpm)
        self.assertEqual(quota.source, "unknown")

    def test_unmapped_model_id_skips_service_quotas_and_returns_unknown(self):
        dynamo = _dynamo_with_item(rpm=None)
        sq = MagicMock()

        quota = fetch_quota_snapshot("some-model-not-in-the-mapping", dynamo_client=dynamo, sq_client=sq)

        self.assertEqual(quota.source, "unknown")
        sq.get_paginator.assert_not_called()

    def test_on_demand_model_uses_on_demand_quota_name(self):
        dynamo = _dynamo_with_item(rpm=None)
        sq = _sq_with_quotas([
            {"QuotaName": "On-demand model inference requests per minute for Qwen3 32B V1", "Value": 1000},
            {"QuotaName": "On-demand model inference tokens per minute for Qwen3 32B V1", "Value": 100_000_000},
        ])

        quota = fetch_quota_snapshot("qwen.qwen3-32b-v1:0", dynamo_client=dynamo, sq_client=sq)

        self.assertEqual(quota.rpm, 1000.0)
        self.assertEqual(quota.tpm, 100_000_000.0)

    def test_table_row_missing_tpm_still_returns_rpm(self):
        """A model synced before TPM support existed (see
        bedrock-runtime-gateway's own history) -- rpm-only is still
        useful, not treated as a full miss."""
        dynamo = _dynamo_with_item(rpm=400, tpm=None)

        quota = fetch_quota_snapshot("us.amazon.nova-micro-v1:0", dynamo_client=dynamo, sq_client=MagicMock())

        self.assertEqual(quota.rpm, 400.0)
        self.assertIsNone(quota.tpm)
        self.assertEqual(quota.source, "table")


if __name__ == "__main__":
    unittest.main()
