# Methodology

> Docs: [methodology](methodology.md) · [SLO statistics](slo-statistics.md) · [quota model](quota-model.md) · [capacity-profile schema](capacity-profile-schema.md) · [experiment design](experiment-design.md) · [correctness history](correctness-history.md) · [README](../README.md)

How a sweep turns requests into metrics: core abstractions, the SLO-goodput headline metric, the measurement window, workload calibration and validation, mixed workloads, and provenance/drift across runs.

## Core abstractions

- **`WorkloadProfile`** (`workload.py`) -- a named input/output token
  shape (e.g. `short_chat` = 512 in / 64 out), defined once in the
  catalog `catalog/workloads.yaml`. Capacity depends heavily on
  this; see `token-sweep.yaml`. Input padding is calibrated per model
  from the provider's own token count (`calibration.py`).
- **`WorkloadMix`** (`workload.py`) -- weighted classes for a mixed-
  workload sweep; each request draws its class independently.
- **`BedrockConverseTarget`** (`client.py`) -- direct Converse/
  ConverseStream calls, no gateway in the path.
- **`RequestResult`** (`results.py`) -- one call, raw (ttft, latency,
  real token counts, outcome). Kept per-request, not just aggregated,
  so any new percentile/SLO/breakdown can be recomputed later without
  spending real Bedrock calls again.
- **Runners** (`runners/`) -- `ConcurrencyRunner` (fixed N closed-loop
  workers: "what does concurrency C do") and `RateRunner` (open-loop
  Poisson at a fixed rps: "what does offered rate R do") -- kept
  separate because they answer different questions and conflating them
  produces a confounded measurement.
- **`analysis/capacity.py`** -- explicit rule-based recommendation, not
  a black-box score: SLO-filter the swept points, pick the one with the
  highest `slo_goodput_rps` among survivors, apply `provider_headroom`.
  Never fits a curve or guesses a number no measured point produced.

## SLO goodput -- the headline metric

Raw throughput is misleading on its own:

```
C    Throughput   TTFT P95   429%    SLO Goodput
1       1.8          420ms    0%        1.8
2       3.1          480ms    0%        3.1
4       4.7          650ms    0%        4.7
6       5.2          910ms    0%        5.1
8       5.4         1600ms    7%        3.9
```

Naive throughput says "C=8 is fastest." This repo says "C=6 is the
recommended concurrency" -- the highest concurrency that still clears
the configured SLO, which is what a gateway config actually needs.

## Measurement policy

Every sweep point runs **warmup -> measurement window -> drain**:

- **warmup** (`warmup_s`): load is applied but nothing is counted.
- **window** (`duration_s`): the only span any metric describes.
- **drain**: load stops at window close; requests still in flight
  finish and are recorded, so their real latency/outcome is kept.

Two populations, each unbiased for what it measures:

- success/throttle/timeout rates and latency/TTFT percentiles use every
  request **scheduled** in the window, drained ones included --
  dropping them would drop exactly the slow tail an SLO catches.
- throughput, token throughput and SLO goodput use successes
  **completed** in the window, divided by the window length.

`repetitions: R` runs each point R times back to back (rate sweeps use
seed+rep, so repetitions are independent Poisson samples). The SLO gate
reads the pooled windows; per-repetition goodput is kept in `evidence`
to show run-to-run spread.

## Input-token calibration

Prompt padding is sized from the **provider's own token count**, not a
chars-per-token guess -- the first real batch sent ~236 tokens for
"512 in", because the repeated filler word tokenizes far denser than
4 chars/token. Before any load is sent, each model's counter is
resolved once (`calibration.py`), in preference order:

| Strategy | How | Cost |
|---|---|---|
| `count_tokens` | Bedrock `CountTokens` -- used whenever the model supports it | free, no inference |
| `converse_usage` | fallback: Converse with `maxTokens=1`, read `usage.inputTokens` | one tiny inference per step |
| `estimate` | last resort (no permission / access): 4 chars ≈ 1 token | -- |

A counter is only accepted if two probes of different length return
increasing counts. Each workload's padding is then rescaled until the
counted input is within `calibration_tolerance_pct` (default 2%) of the
target -- typically 2-4 steps. Calibration runs before warmup and is
never part of a measurement window. Inference-profile ids
(`us.`/`eu.`/...) are retried as their base model id for CountTokens.

Per model, `token_counting` in `catalog/models.yaml` can force a strategy
(`auto` by default). As of 2026-09-25 **none of the five certified
models support CountTokens** (Bedrock: "The provided model doesn't
support counting tokens"), so they all calibrate via `converse_usage`;
a model that gains support switches to `count_tokens` automatically.

## Workload validation

Calibration sizes the input; `max_tokens` only caps the output -- the
model may emit far less. Asking for "about N words" wasn't enough: on
nova-micro it produced only 42-50% of the target on every workload, all
ending `end_turn`. The prompt therefore asks for ~2x the budget and
forbids wrapping up, so generation ends on `max_tokens` -- measured
after the fix: 100% of requests hit exactly 64 / 64 / 256 / 1024 output
tokens across the four token-sweep shapes. Every result records
Bedrock's `stop_reason` (`max_tokens` vs `end_turn`) in the raw JSONL.

Each class's `workload_validation` still checks BOTH sides against what
Bedrock actually *reported* during the run:

```yaml
workload_validation:
  token_counting: {method: converse_usage, calibrated_input_tokens: 509, converged: true, iterations: 3}
  input:  {target: 4096, observed_p50: 4090, deviation_pct: -0.15, tolerance_pct: 10.0, valid: true}
  output: {target: 512,  observed_p50: 438,  deviation_pct: -14.45, tolerance_pct: 25.0, valid: true}
  valid: true              # both
```

`valid: false` means the envelope describes a different workload shape
than the class name claims (e.g. "4096 in / 512 out" that really
emitted 110 tokens) -- `run.py` warns, and the pilot (`run_all.py --pilot`) fails the
workload before a batch spends time on it. Tolerances: `workload_validation_tolerance_pct`
(input, default 10) and `output_validation_tolerance_pct` (default 25 --
models legitimately stop a little early).

## Mixed workloads

`experiments/mixed-capacity.yaml` sweeps ONE offered rate where each
arrival draws its class by weight (60% short_chat / 30% rag_answer /
10% long_generation), so
classes genuinely overlap in flight. A point passes only if the SLO
holds for the blend **and every class** -- a blended p95 can look fine
while the long class alone blows its latency SLO. The artifact gains:

```yaml
mixed_workloads:
  short70_long30:
    shares: {short_chat: 0.6, rag_answer: 0.3, long_generation: 0.1}
    rate: {observed_nonfailing_offered_rps: ..., observed_verdict: ..., statistically_confirmed_offered_rps: ...}
    recommendation: {admission_envelope: {sustained_rps: ..., ...}}
    evidence: {...}
    classes_at_recommended_point: {short_chat: {...}, rag_answer: {...}, long_generation: {...}}
```

It's valid for that mix's shares only -- a different traffic mix
needs its own run. Classes measured only inside a mix get
`observed`/`workload_validation`, never an isolated envelope.

## Provenance and drift

A profile is ONE snapshot of the Bedrock inference-profile operating
envelope under the provider conditions at measurement time. The safe
rate measured today can be 5.0 rps, tomorrow 4.2, tonight 5.8 -- so a
single `capacity-profile.yaml` is not a permanent fact. Every profile
records where and when it came from:

```yaml
environment:
  measured_at: {start: 2026-09-26T13:39:11+00:00, end: ...}
  account: "646821141010"
  region: us-east-1
  inference_profile: us.amazon.nova-micro-v1:0
  benchmark_version: 0.1.0
  git_commit: 451f7d4...
  git_dirty: false
  runtime: {python: 3.11.16, boto3: ..., botocore: ...}
validity:
  repeated_runs: 1
  days_observed: 1
  scope: "single run -- ... compare with scripts/drift.py"
```

`scripts/drift.py` lines repeated profiles up per (model, experiment,
workload/mix, sweep kind) and reports runs, days observed, the confirmed
and production values over time (oldest first), min / median / max,
their spread, whether the envelope is `stable` (spread <= `--threshold`,
default 20%), and a `conservative_production` (the minimum across runs):

```bash
.venv/bin/python scripts/drift.py results/
```

Re-measuring on different days and times of day turns a snapshot into
evidence about temporal stability -- and into a basis for choosing how
often an envelope must be re-certified.
