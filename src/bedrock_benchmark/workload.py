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
recorded on RequestResult. calibration.py replaces the estimate with
a padding length measured by the provider itself (`filler_chars`) --
Bedrock CountTokens where the model supports it, else a Converse
usage probe -- and report.py's workload_validation checks the real
counts against the requested shape either way.

WorkloadMix -- a weighted set of profiles for mixed-workload
experiments: each request independently samples its class, so short
and long traffic genuinely overlap in flight, which is the only way to
measure what isolated per-class sweeps can't (see report.py on why no
global number is ever derived from isolated maxima).
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

DEFAULT_WORKLOADS_FILE = "catalog/workloads.yaml"

_CHARS_PER_TOKEN_ESTIMATE = 4
# Ask for ~1.5 words per budgeted token (~2x the budget in tokens) so the
# model runs into max_tokens instead of ending early -- see prompt().
_OUTPUT_WORDS_PER_TOKEN_ASKED = 1.5
_FILLER_WORD = "benchmark "  # 10 chars incl. space -- deliberately plain, no semantic content to bias the model


@dataclass
class WorkloadProfile:
    name: str
    input_tokens: int
    output_tokens: int  # used as max_tokens on the request -- the model may emit fewer
    # SLO profile from constraints/slo.yaml (TTFT/TPOT/success/throttle),
    # bound in the catalog.
    slo_profile: Optional[str] = None
    # Workload-level end-to-end sanity cap, p95 -- business-defined per
    # workload, because a 64-, 256- and 1024-token output can't share
    # one E2E budget. Overrides any latency_p95_ms in the profile.
    latency_p95_ms: Optional[float] = None
    # Padding length in chars, set by calibration.py from a provider
    # token count. None = the 4-chars/token estimate.
    filler_chars: Optional[int] = None

    def estimated_filler_chars(self) -> int:
        return self.input_tokens * _CHARS_PER_TOKEN_ESTIMATE

    def prompt(self) -> str:
        # Output length: capping max_tokens alone doesn't ELICIT output, and
        # asking for "about N words" doesn't either -- models stop early
        # (end_turn) well below the budget: measured on nova-micro, "about
        # output_tokens/2 words" produced only 42-50% of the target on every
        # workload. So the prompt asks for clearly MORE text than the budget
        # (~2x in tokens) and forbids wrapping up; generation then ends on
        # max_tokens, which is exactly the workload shape we mean to
        # measure. workload_validation + RequestResult.stop_reason check it.
        target_chars = self.filler_chars if self.filler_chars is not None else self.estimated_filler_chars()
        target_chars = max(0, target_chars)
        repeats = max(1, target_chars // len(_FILLER_WORD) + 1)
        filler = (_FILLER_WORD * repeats)[:target_chars].strip()
        target_words = max(20, int(self.output_tokens * _OUTPUT_WORDS_PER_TOKEN_ASKED))
        return (
            f"{filler}\n\nIgnore the text above; it is padding to reach a target input "
            f"length. Write a long, continuous, detailed essay of at least {target_words} "
            f"words on the history of maritime navigation. Keep writing until you reach "
            f"that length: do not summarize, do not conclude, and do not stop early."
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


def load_workloads(path: str = DEFAULT_WORKLOADS_FILE) -> Dict[str, WorkloadProfile]:
    """The workload catalog: name -> WorkloadProfile. Every workload
    binds an slo_profile explicitly; whether that profile exists is
    checked against the SLO file when an experiment loads."""
    raw = yaml.safe_load(Path(path).read_text()) or {}
    entries = raw.get("workloads")
    if not isinstance(entries, dict) or not entries:
        raise ValueError(f"{path}: expected `workloads: {{<name>: {{input_tokens, output_tokens, slo_profile}}}}`")
    catalog = {}
    for name, cfg in entries.items():
        cfg = dict(cfg or {})
        if "name" in cfg:
            raise ValueError(f"{path}: {name}: the key is the name -- drop `name:`")
        workload = WorkloadProfile(name=name, **cfg)
        if not workload.slo_profile:
            raise ValueError(f"{path}: workload {name!r} needs an explicit slo_profile")
        if workload.latency_p95_ms is not None and workload.latency_p95_ms <= 0:
            raise ValueError(f"{path}: workload {name!r} latency_p95_ms must be > 0")
        if workload.input_tokens <= 0 or workload.output_tokens <= 0:
            raise ValueError(f"{path}: workload {name!r} token counts must be > 0")
        catalog[name] = workload
    return catalog
