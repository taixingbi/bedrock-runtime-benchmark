"""Raw per-request JSONL, one line per RequestResult -- see results.py's
own docstring on why raw results, not just aggregates, are worth
keeping: any new percentile, SLO threshold, or outcome breakdown can be
recomputed later from this file without spending real Bedrock calls
again.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import List

from .results import RequestResult


def write_jsonl(results: List[RequestResult], path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for result in results:
            f.write(json.dumps(asdict(result), default=str))
            f.write("\n")


def read_jsonl(path: str) -> List[RequestResult]:
    results = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                results.append(RequestResult(**json.loads(line)))
    return results
