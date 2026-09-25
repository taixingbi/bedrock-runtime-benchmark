import tempfile
import unittest
from pathlib import Path

from bedrock_benchmark.results import RequestResult
from bedrock_benchmark.storage import read_jsonl, write_jsonl


class JsonlRoundTripTests(unittest.TestCase):
    def test_write_then_read_preserves_fields(self):
        results = [
            RequestResult(
                request_id="a", scheduled_at=1.0, started_at=1.1, completed_at=1.2,
                success=True, latency_ms=100.0, input_tokens=10, output_tokens=5,
            ),
            RequestResult(
                request_id="b", scheduled_at=2.0, started_at=2.1, completed_at=2.2,
                success=False, error="boom", error_code="ThrottlingException", throttled=True,
            ),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "nested" / "results.jsonl")
            write_jsonl(results, path)
            loaded = read_jsonl(path)

        self.assertEqual(len(loaded), 2)
        self.assertEqual(loaded[0].request_id, "a")
        self.assertEqual(loaded[0].input_tokens, 10)
        self.assertTrue(loaded[1].throttled)
        self.assertEqual(loaded[1].error_code, "ThrottlingException")

    def test_creates_parent_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "a" / "b" / "c.jsonl")
            write_jsonl([], path)
            self.assertTrue(Path(path).exists())


if __name__ == "__main__":
    unittest.main()
