"""WorkloadProfile -- a named input/output token shape (e.g. "short" =
512 input / 64 output, "long" = 4096 input / 512 output). Capacity
depends heavily on this: a model's safe concurrency for short-short
traffic can be very different from long-long (see token-sweep.yaml) --
this repo never assumes one workload shape represents "the" capacity
of a model.

`prompt()` generates a real input string of approximately
input_tokens length using the same ~4-chars-per-token heuristic
bedrock-runtime-gateway's own usage/token_estimate.py uses -- it only
needs to be CLOSE, not exact: the real input_tokens actually consumed
comes back from Bedrock's own response usage block and is what's
recorded on RequestResult, never this estimate.
"""
from __future__ import annotations

from dataclasses import dataclass

_CHARS_PER_TOKEN_ESTIMATE = 4
_FILLER_WORD = "benchmark "  # 10 chars incl. space -- deliberately plain, no semantic content to bias the model


@dataclass
class WorkloadProfile:
    name: str
    input_tokens: int
    output_tokens: int  # used as max_tokens on the request -- the model may emit fewer

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
