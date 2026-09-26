"""Input-token calibration -- sizes each workload's prompt padding from
the PROVIDER's own token count instead of trusting 4 chars ~= 1 token.

Tokenizers differ per model, and the padding text matters: the first
real batch sent ~236 tokens for "512 in" because the repeated filler
word tokenizes far denser than 4 chars/token. A class that claims
"512 in" but measures 236 describes a different workload.

Strategy, resolved ONCE per model before any load is sent:

1. count_tokens   -- Bedrock CountTokens, if the model supports it:
                     free (no inference) and the model's own tokenizer.
2. converse_usage -- otherwise, a Converse call with maxTokens=1,
                     reading usage.inputTokens: the provider's real
                     count for that exact input, for one tiny inference
                     per calibration step. Works on every model.
3. estimate       -- only if neither works (no permission, no access):
                     the 4-chars/token padding, recorded as such.

A counter is accepted only if it's RESPONSIVE: two probes of different
length must return different, increasing counts -- a counter that
returns a constant can't size anything. Then per workload:

    desired 512 -> estimated padding -> count -> rescale padding -> ... -> ~512

Token count is close to linear in padding chars, so rescaling
chars *= target/counted converges in a few steps; the closest attempt
is kept if it never lands inside tolerance (e.g. the fixed instruction
suffix alone exceeds a tiny target). Every result is recorded in the
artifact, and report.py's workload_validation independently checks the
counts Bedrock reported during the run.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable, List, Optional, Tuple

from .workload import WorkloadProfile

CountFn = Callable[[str], int]

STRATEGIES = ("auto", "count_tokens", "converse_usage", "estimate")


@dataclass
class CalibrationResult:
    profile: WorkloadProfile  # a copy with filler_chars set, or unchanged for "estimate"
    method: str  # "count_tokens" | "converse_usage" | "estimate"
    counted_input_tokens: Optional[int] = None
    iterations: int = 0
    converged: bool = False
    note: Optional[str] = None  # why a strategy was skipped / failed

    def to_dict(self) -> dict:
        out = {
            "method": self.method,
            "calibrated_input_tokens": self.counted_input_tokens,
            "converged": self.converged if self.method != "estimate" else None,
            "iterations": self.iterations,
        }
        if self.note:
            out["note"] = self.note
        return out


def _describe(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"


def probe_counter(count_fn: CountFn) -> Optional[str]:
    """None if count_fn works AND responds to input length, else why not."""
    try:
        short = count_fn("benchmark probe")
        long = count_fn("benchmark probe " * 200)
    except Exception as exc:  # noqa: BLE001 - any failure means "try the next strategy", recorded
        return _describe(exc)
    if not long > short:
        return f"counts don't grow with input ({short} -> {long}); unusable for sizing"
    return None


def resolve_counter(
    strategy: str, counters: List[Tuple[str, CountFn]],
) -> Tuple[Optional[str], Optional[CountFn], List[str]]:
    """Picks the first responsive counter in preference order, or the
    one `strategy` forces. Returns (method, count_fn, notes) -- method
    None means fall back to the estimate."""
    if strategy not in STRATEGIES:
        raise ValueError(f"token_counting must be one of {STRATEGIES}, got {strategy!r}")
    if strategy == "estimate":
        return None, None, ["token_counting: estimate (configured)"]
    notes = []
    for name, fn in counters:
        if strategy not in ("auto", name):
            continue
        reason = probe_counter(fn)
        if reason is None:
            return name, fn, notes
        notes.append(f"{name} unavailable: {reason}")
    return None, None, notes


def calibrate_profile(
    profile: WorkloadProfile, count_fn: CountFn, method: str, *,
    tolerance_pct: float = 2.0, max_iterations: int = 8,
) -> CalibrationResult:
    target = profile.input_tokens
    chars = profile.estimated_filler_chars()
    max_chars = max(1000, target * 40)  # sanity cap: no real tokenizer is < 1 token per 40 chars
    best: Optional[tuple] = None  # (abs_error, chars, counted)

    for i in range(1, max_iterations + 1):
        candidate = replace(profile, filler_chars=chars)
        try:
            counted = count_fn(candidate.prompt())
        except Exception as exc:  # noqa: BLE001 - a mid-calibration failure falls back, visibly
            return CalibrationResult(profile=profile, method="estimate", iterations=i - 1,
                                     note=f"{method} failed mid-calibration: {_describe(exc)}")
        error = abs(counted - target)
        if best is None or error < best[0]:
            best = (error, chars, counted)
        if error <= target * tolerance_pct / 100.0:
            return CalibrationResult(profile=candidate, method=method, counted_input_tokens=counted,
                                     iterations=i, converged=True)
        if counted <= 0:
            break
        new_chars = max(0, min(max_chars, round(chars * target / counted)))
        if new_chars == chars:
            new_chars = max(0, min(max_chars, chars + (1 if counted < target else -1) * max(1, chars // 100)))
        if new_chars == chars:
            break
        chars = new_chars

    _, best_chars, best_counted = best
    return CalibrationResult(
        profile=replace(profile, filler_chars=best_chars), method=method,
        counted_input_tokens=best_counted, iterations=max_iterations, converged=False,
        note=f"closest achievable was {best_counted} tokens for a {target} target",
    )


def estimate_profile(profile: WorkloadProfile, note: str) -> CalibrationResult:
    return CalibrationResult(profile=profile, method="estimate", note=note)
