"""WorkloadProfile -- a named input/output token shape (e.g. "short" =
512 input / 64 output, "long" = 4096 input / 512 output). Capacity
depends heavily on this: a model's safe concurrency for short-short
traffic can be very different from long-long (see token-sweep.yaml) --
this repo never assumes one workload shape represents "the" capacity
of a model.

`prompt()` generates a real input string of approximately
input_tokens length using the same ~4-chars-per-token estimate
bedrock-runtime-gateway's own usage/token_estimate.py uses -- it only
needs to be CLOSE, not exact: the real input_tokens actually consumed
comes back from Bedrock's own response usage block and is what's
recorded on RequestResult, never this estimate. report.py's
workload_validation compares that real count against the requested
shape, so a model whose tokenizer strays far from 4 chars/token is
flagged rather than silently mislabeled.

WorkloadMix -- a weighted set of profiles for mixed-workload
experiments: each request independently samples its class, so short
and long traffic genuinely overlap in flight, which is the only way to
measure what isolated per-class sweeps can't (see report.py on why no
global number is ever derived from isolated maxima).
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

_CHARS_PER_TOKEN_ESTIMATE = 4
_FILLER_WORD = "benchmark "  # 10 chars incl. space -- deliberately plain, no semantic content to bias the model


@dataclass
class WorkloadProfile:
    name: str
    input_tokens: int
    output_tokens: int  # used as max_tokens on the request -- the model may emit fewer
    # Named SLO from the experiment's slo_profiles (None = the default
    # `slo:`) -- a 512-token generation shouldn't be held to the same
    # end-to-end latency as a 64-token one.
    slo_profile: Optional[str] = None

    def prompt(self) -> str:
        # Explicitly asks for output of roughly the target length --
        # capping max_tokens alone doesn't ELICIT a long response, and
        # a workload class meant to characterize long-output behavior
        # (e.g. token-sweep.yaml's 512-output profile) needs the model
        # to actually try to fill that budget, not stop early because
        # the prompt itself only warranted a one-line reply.
        target_chars = self.input_tokens * _CHARS_PER_TOKEN_ESTIMATE
        repeats = max(1, target_chars // len(_FILLER_WORD))
        filler = (_FILLER_WORD * repeats)[:target_chars].strip()
        target_words = max(1, self.output_tokens // 2)  # ~2 tokens/word, rough
        return (
            f"{filler}\n\nIgnore the text above; it is padding to reach a target input "
            f"length. Write approximately {target_words} words of any plausible "
            f"filler content on a neutral topic."
        )

    def sample(self, rng: random.Random) -> "WorkloadProfile":
        """A single profile is a one-class mix -- lets runners treat
        both uniformly."""
        return self


@dataclass
class WorkloadMix:
    name: str
    entries: List[Tuple[WorkloadProfile, float]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.entries:
            raise ValueError(f"mix {self.name!r} has no entries")
        if any(w <= 0 for _, w in self.entries):
            raise ValueError(f"mix {self.name!r} weights must all be > 0")

    @property
    def shares(self) -> dict:
        """Normalized weights -- what fraction of offered load each class gets."""
        total = sum(w for _, w in self.entries)
        return {p.name: w / total for p, w in self.entries}

    def sample(self, rng: random.Random) -> WorkloadProfile:
        profiles = [p for p, _ in self.entries]
        weights = [w for _, w in self.entries]
        return rng.choices(profiles, weights=weights, k=1)[0]
