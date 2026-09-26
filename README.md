# bedrock-runtime-benchmark

`bedrock-runtime-benchmark` characterizes Bedrock model capacity under
controlled workloads and derives SLO-aware operating envelopes for
runtime admission and concurrency configuration.

It does not test the whole gateway, and it does not do release
regression -- that's `bedrock-platform-eval`'s job. It answers exactly one
question:

> For a given Bedrock model/inference profile and workload shape, at a
> given SLO, what's the safe concurrency/RPS/token envelope?

## Boundary

Every call in this repo goes **directly to Bedrock** (`boto3`
Converse/ConverseStream) -- no API Gateway, no auth, no gateway
admission control, no tenant quota, no gateway queue. Mixing those in
would measure *platform* capacity, not *model/backend* capacity, and
this repo only ever answers the second question.

```
              bedrock-runtime-benchmark

      Experiment / Sweep Engine
      Workload Generator (WorkloadProfile)
      Concurrency Runner / Rate Runner
      Bedrock Runtime Client (direct boto3)
      Metrics + Capacity Analyzer + Recommendation Engine
                    |
                    v
              AWS Bedrock (direct)
```

Three sibling repos, three different questions:

| Repo | Question |
|---|---|
| `bedrock-runtime-gateway` | Is the gateway's own implementation correct? |
| `bedrock-platform-eval` | Does the *deployed platform* (gateway + Bedrock) behave correctly under real workload? |
| `bedrock-runtime-benchmark` | What's the *model's own* safe operating envelope, independent of any gateway? |

This repo's output feeds the first two as **input**, not as a replacement
for either: a gateway's per-tenant-class concurrency config should be
*informed* by a measured capacity-profile.yaml, not guessed.

## Core abstractions

- **`WorkloadProfile`** (`workload.py`) -- a named input/output token
  shape (e.g. "short" = 512 in / 64 out). Capacity depends heavily on
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

## Models, experiments and constraints

Three independent inputs, combined at run time:

```
scripts/models.yaml        WHICH models: name (= results folder), model_id, region
experiments/*.yaml         WHAT load: workloads + sweep -- no model, no SLO, no quota
constraints/
  ├─ slo.yaml              SLO:   what quality we REQUIRE  (per traffic class)
  └─ quota.yaml            quota: what the provider ALLOWS (per model)
        ↓
every experiment x every model -> capacity-profile.yaml (judged against the constraints)
```

- **Models** -- `scripts/models.yaml`: the five models
  `bedrock-runtime-gateway` certifies (nova-micro, nova-lite, nova-pro,
  llama3-3-70b, qwen3-32b). `enabled: false` skips one by default.
- **Experiments** -- `experiments/*.yaml`: model-agnostic workload +
  sweep definitions. Every experiment runs unchanged against every model.
- **Constraints** -- `constraints/`: each number defined exactly once.
  The loaders reject SLO or quota numbers anywhere else (an experiment
  with `slo:`/`target:`/`quota:`, a models entry with `quota:`), so no
  copy can drift and every run of the same workload class is judged
  the same way. `--slo-file` / `--quota-file` point a run at other
  constraint files (e.g. a stricter SLO).

Rate sweeps are written as `quota_fractions` of each sweep subject's
**provider ceiling** -- the request rate the model's quota allows for
that workload's token shape (`ceiling.py`):

```
rpm_rps = RPM / 60
tpm_rps = TPM / (input_tokens + max_tokens x output_burndown) / 60
ceiling = min(rpm_rps, tpm_rps)
```

Quotas cap requests AND tokens, and which binds depends on the
workload (a short request on nova-micro is RPM-bound; a long one on a
tight-TPM model can be TPM-bound). `max_tokens`, not actual output,
counts because Bedrock reserves input + max_tokens against TPM when a
request starts. `output_burndown` (models file, default 1) covers models
that bill output tokens at a multiple. Every class/mix in the artifact
records it:

```yaml
provider_constraints:
  tokens_per_request: 576.0
  rpm_rps_ceiling: 6.6667
  tpm_rps_ceiling: 231.4815
  ceiling_rps: 6.6667
  binding_constraint: rpm      # or tpm
sweep_values_rps: [1.6667, 3.3333, ...]
```

Quotas differ by an order of magnitude (50 RPM for nova-pro, 1000 for
qwen3-32b), so the same `[0.25 .. 2.5]` sweep is 0.21-2.08 rps on
nova-pro and 4.2-41.7 rps on qwen3-32b. Concurrency sweeps stay
absolute (and may not exceed `transport.max_connections`).

| Experiment | Sweep | Per model |
|---|---|---|
| `concurrency-sweep.yaml` | concurrency 1/2/4/6/8, short workload | ~11 min |
| `rate-capacity.yaml` | 0.25x-2.5x quota, short workload -- the canonical production-envelope run | ~13 min |
| `mixed-capacity.yaml` | 0.25x-2.5x quota, 70% short / 30% long_long | ~13 min |
| `token-sweep.yaml` | 4 input/output shapes x concurrency 1/2/4/6 | ~27 min |

Keep `constraints/quota.yaml` current with `scripts/fetch_quota.py --all` (see
"Quota-aware experiment design" below) -- a stale quota shifts every
rate a quota-relative sweep tests.

## Running experiments

```bash
python3.11 -m venv .venv && .venv/bin/pip install -e ".[dev]"   # same install CI uses

# one experiment
.venv/bin/python scripts/run.py experiments/concurrency-sweep.yaml                     # every enabled model
.venv/bin/python scripts/run.py experiments/concurrency-sweep.yaml --model nova-micro  # one model

# everything: every experiment x every enabled model
.venv/bin/python scripts/run_all.py --dry-run          # validate all pairs + time estimate, no AWS calls
.venv/bin/python scripts/run_all.py                    # 4 experiments x 5 models ~= 5.5h
.venv/bin/python scripts/run_all.py --model nova-micro --model nova-pro
.venv/bin/python scripts/run_all.py experiments/rate-capacity.yaml
.venv/bin/python scripts/run_all.py --gateway-config my-gateway.yaml   # + gateway diff at the end
```

Results are grouped by model:

```
results/run-all-<timestamp>/        # run.py: results/
  nova-micro/
    concurrency-sweep-<id>.jsonl
    concurrency-sweep-<id>-capacity-profile.yaml
    ...
  nova-pro/
    ...
  summary.yaml
```

Runs are strictly sequential, grouped by model -- runs against the same
model share its quota, so parallel runs would measure each other's load
as throttling. Every (model, experiment) pair is validated before the
first call; a failed run doesn't stop the rest (`--fail-fast` to stop).
Exit code is non-zero if any run failed or the gateway diff has warn
findings.

Each run writes raw per-request JSONL and a `capacity-profile.yaml`
artifact under `results/` (gitignored -- these are real measurement outputs, not
checked-in fixtures). The `capacity-profile.yaml` schema (v5 -- see
"Correctness fixes" below for why `rate` and `concurrency` are always
kept in separate blocks, why the rate block separates offered load
from goodput, and why there's no `global_max_concurrency`). A
rate-capacity result:

```yaml
schema_version: 5
experiment: rate-capacity
model: {name: nova-micro, provider: bedrock, model_id: ..., region: ...}
constraints:                                       # what every number was judged against
  quota: {rpm: 400, tpm: 8000000, output_burndown: 1.0}                     # constraints/quota.yaml
  slo:                                                                       # constraints/slo.yaml
    default: interactive
    profiles:
      interactive: {ttft_p95_ms: 1000, latency_p95_ms: 3000, success_rate_min: 0.99, throttle_rate_max: 0.001, confidence: null}
      long_generation: {ttft_p95_ms: 1000, latency_p95_ms: 10000, ...}
measurement:
  warmup_s: 10
  window_s: 90
  repetitions: 1
  window_policy: scheduled_in_window_for_rates__completed_in_window_for_throughput
  clock: monotonic_durations__wall_clock_timestamps
  gate: point_estimate                      # or confidence_bound when slo.confidence is set
  min_requests_to_resolve_throttle_slo: 2703
sweep: {type: rate, quota_fractions: [0.25, ...], relative_to: provider_ceiling}
workload_classes:
  short:
    slo_profile: interactive
    observed: {input_tokens_p50: 505, output_tokens_p50: 61}
    workload_validation: {input: {...}, output: {...}, valid: true}
    provider_constraints: {tokens_per_request: 576.0, ceiling_rps: 6.6667, binding_constraint: rpm, ...}
    sweep_values_rps: [1.6667, 3.3333, ...]
    rate:
      max_safe_offered_rps: 8.3333          # the swept offered rate that passed the SLO
      slo_goodput_rps: 8.1                  # what it actually delivered within SLO
      saturation_offered_rps: 10.0
      saturation_status: resolved           # or not_reached / unresolved (+ unstable_region)
      production_offered_rps: 6.6667        # headroom applied to OFFERED rate -- what a gateway limit reads
    evidence: {n: 745, n_throttled: 0, throttle_rate_upper: 0.0036, peak_outstanding: 9, ...}
provider: {headroom: 0.20}
transport: {max_connections: 64, executor_workers: 64, total_max_attempts: 1, connect_timeout_s: 5, read_timeout_s: 60}
```

A concurrency sweep writes `concurrency: {measured_best, saturation,
saturation_status, production_max, slo_goodput_rps}` instead of `rate`.

This is the actual deliverable -- not an HTML report. A gateway's own
config review reads this file, and decides its own global/tenant/AIMD
config FROM these per-class envelopes -- this repo never pre-packages
a gateway control policy itself (see "Not in scope here" below).

## Correctness fixes (schema v2 / v3 / v4)

A real review caught 5 measurement-correctness bugs before this
artifact was ever used to actually inform a gateway config:

1. **Rate vs. concurrency saturation were the same field.** A rate
   sweep's saturation point (an RPS value) used to be written into
   `saturation_concurrency` -- silently mislabeling e.g. "7 rps" as if
   it were a concurrency value. `rate` and `concurrency` are now
   always separate blocks with their own field names
   (`saturation_rps` vs `saturation`), and `provider_headroom` is
   applied to whichever one actually ran (`production_rps` is a float,
   never floored -- `production_max` is an int floor, since fractional
   concurrency isn't meaningful).
2. **A missing TTFT measurement counted as meeting a configured TTFT
   SLO.** `ttft_slo_ms is not None and r.ttft_ms is not None and ...`
   skipped the check entirely when `ttft_ms` was `None` (e.g.
   `stream: false` with a TTFT SLO configured anyway) -- silently
   treating an unmeasured request as SLO-compliant. Fixed to fail
   closed: a configured-but-unmeasured SLO is a violation.
3. **`RateRunner`'s `scheduled_at` was captured AFTER its own
   `asyncio.sleep`**, making it drift to `~= started_at` and destroying
   the one signal it exists for -- client-side scheduling lag
   (`started_at - scheduled_at`) when the load generator itself falls
   behind its own arrival schedule under high offered rate. Now
   captured before the sleep, from a wall-clock anchor taken at the
   same instant as the run's own `perf_counter` baseline.
4. **No explicit boto3 transport/retry config.** Sweeping concurrency
   up past boto3's default connection pool (10) would measure the
   SDK's own queueing, not Bedrock's -- and the SDK's automatic retry
   would silently absorb a real `ThrottlingException` into an eventual
   200, understating the real throttle rate this repo exists to
   measure. `TransportConfig` (`client.py`) now sets pool size,
   timeouts, and retries explicitly, and records the config used into
   the artifact for reproducibility.
5. **`global_max_concurrency = max(concurrencies)` across independently
   swept workload classes was invalid.** There's no scientifically
   defensible "global" number derivable from isolated per-class
   maxima -- a real MIXED workload can exceed safe capacity before
   either class's own isolated measurement would predict. Removed
   entirely; a real mixed-workload experiment (`mix:`, see "Mixed
   workloads" below) is the only valid way to answer that question.
6. **`retries={"max_attempts": 1}` was still a retry, not zero.**
   botocore's client-config normalization (`botocore/args.py`,
   `_compute_retry_max_attempts`) treats a `max_attempts` key as
   meaning *retry* attempts and rewrites it to
   `total_max_attempts = max_attempts + 1` before building the retry
   handler -- so the old default actually allowed 1 initial request
   + 1 retry = 2 total attempts. A real Bedrock 429 could still be
   silently retried into an eventual 200, understating the exact
   throttle rate this repo exists to measure. Fixed by passing
   `total_max_attempts` (botocore's own unambiguous "literal total
   attempt count" key) instead; `TransportConfig.total_max_attempts`
   replaces the old `retry_max_attempts` field name.

### Schema v3 fixes

7. **Drain completions inflated throughput.** `ConcurrencyRunner`'s
   last batch (fired just before `duration_s`) finished after it, yet
   `throughput = successes / duration_s` counted those completions in
   the numerator without extending the denominator -- overstating
   throughput and SLO goodput by up to one concurrency level's worth
   of requests per point. Fixed by the measurement policy below.
8. **The rate block mixed goodput with offered load.**
   `measured_sustainable_rps` was the best point's SLO goodput, and
   `production_rps` applied headroom to that goodput -- but a gateway
   admission limit is on offered load. Split into
   `max_safe_offered_rps` / `slo_goodput_rps` /
   `production_offered_rps`.
9. **A 0.1% throttle SLO was gated on too few samples.** See
   "Statistical resolution" below.

### Schema v4 fixes

10. **`asyncio.to_thread`'s default executor was a hidden capacity
    limit.** It has min(32, cpu_count + 4) workers -- 14 on a 10-core
    laptop -- independent of the 64-connection pool, so past ~14
    in-flight calls requests queued for a Python thread and the
    benchmark measured the thread pool. The target now owns a
    `ThreadPoolExecutor` sized `transport.executor_workers` (default =
    `max_connections`, must be >=), tracks peak outstanding calls per
    point, and a point that ever exceeded the pool is
    `client_limited` -- excluded from the recommendation and listed in
    `client_limited_points`. `executor_workers` is recorded in the
    artifact.
11. **Latency/TTFT used the wall clock.** `time.time()` jumps with NTP
    corrections. Durations now come from `time.perf_counter()`; wall
    timestamps are one anchor + monotonic deltas.
12. **Rate sweeps ignored TPM.** See "provider ceiling" above.
13. **Output tokens weren't validated.** See "Workload validation".
14. **A non-monotonic sweep produced contradictory results.** PASS,
    PASS, FAIL, PASS, FAIL reported best=C6 with saturation=C4. Now
    `saturation_status` is `resolved` (clean pass->fail; saturation =
    first fail), `not_reached` (all passed), or `unresolved` (a pass
    after a fail: no saturation claimed; `unstable_region` and
    `confirmed_fail_from` instead). Only the leading run of passes is
    eligible for the recommendation.
15. **One SLO for every workload.** See "SLO profiles".
16. **Input padding trusted 4 chars ≈ 1 token.** Real runs measured
    ~46% of the requested input. Padding is now calibrated per model
    from the provider's own count -- see "Input-token calibration".
17. **SLO and quota numbers were copied into every file.** The same
    `slo:` block lived in 4 experiments and quotas in the models list.
    Both now live once under `constraints/` (schema v5 groups them in
    the artifact's `constraints:` block); loaders reject copies.

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

**Statistical resolution of the throttle SLO.** A 0.1% throttle SLO
can't be demonstrated from a few hundred requests: 0 throttles out of
540 (6 rps x 90s) has a 95% one-sided Wilson upper bound of ~0.5%.
Resolving 0.1% at 95% needs ~2,700 measured requests per point.
Every point therefore records `throttle_rate_upper` /
`success_rate_lower`, and:

- `slo.confidence` unset (default): gate on point estimates, as before;
  `scripts/run.py` warns up front for rate points that can't reach the
  required sample size.
- `slo.confidence: 0.95`: gate on the bounds -- an under-sampled point
  fails closed. Raise `duration_s` x `repetitions` to match (e.g.
  6 rps needs ~450s of pooled window).

## Quota-aware experiment design

Picking sane sweep values (especially rate-sweep values) is guesswork
without knowing the model's real RPM/TPM ceiling first -- a
concurrency sweep starting at `[1, 2, 4, ...]` is useless if even
concurrency=1 closed-loop already runs over quota, which turns out to
be true for some certified models. `scripts/fetch_quota.py` and
`src/bedrock_benchmark/quota.py` exist to make that ceiling a known
number before an experiment is written, not a name for it to just do.

```bash
.venv/bin/python scripts/fetch_quota.py --all                                 # check constraints/quota.yaml
.venv/bin/python scripts/fetch_quota.py --model-id us.amazon.nova-pro-v1:0    # one model, for a new entry
```

`--all` compares each entry in `constraints/quota.yaml` with the live
value and prints a replacement line for any stale one (exit 1 if any
differ). It looks up the model's real RPM/TPM in two steps, table first:

1. `gateway-model-quotas-dev`'s `quota#<model_id>` row, if that table
   happens to be reachable -- a cheap `GetItem` against a value
   `bedrock-runtime-gateway` already synced from AWS. A soft
   convenience: this repo doesn't provision that table and doesn't
   assume it exists.
2. AWS Service Quotas directly (`service-quotas:ListServiceQuotas`),
   the actual source of truth, whenever the table lookup fails for
   any reason (table missing, row missing, no permission, wrong
   account/region).

If neither source is available it returns `source: unknown` rather
than raising -- a missing quota number should never block an
experiment design conversation, it should just make the gap visible.
This is read-only, design-time context: nothing at runtime checks or
caps against it (see "Not in scope here" below).

Why rate sweeps are quota-relative: nova-pro's 50 RPM (0.83 rps) with
~616ms latency means concurrency=1 closed-loop already runs ~2x over
quota, and llama3-3-70b's 80 RPM sits right at its C=1 rate -- a
concurrency sweep can't resolve either model's safe zone, and a fixed
rps list tuned for one model is useless for another. The first full
batch found real ceilings between ~1.0x and ~1.9x quota, so the shipped
sweeps span 0.25x-2.5x.

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

Per model, `token_counting` in `scripts/models.yaml` can force a strategy
(`auto` by default). As of 2026-09-25 **none of the five certified
models support CountTokens** (Bedrock: "The provided model doesn't
support counting tokens"), so they all calibrate via `converse_usage`;
a model that gains support switches to `count_tokens` automatically.

## Workload validation

Calibration sizes the input; `max_tokens` only caps the output -- the
model may emit far less. So each class's `workload_validation` checks
BOTH sides against what Bedrock actually *reported* during the run:

```yaml
workload_validation:
  token_counting: {method: converse_usage, calibrated_input_tokens: 509, converged: true, iterations: 3}
  input:  {target: 4096, observed_p50: 4090, deviation_pct: -0.15, tolerance_pct: 10.0, valid: true}
  output: {target: 512,  observed_p50: 438,  deviation_pct: -14.45, tolerance_pct: 25.0, valid: true}
  valid: true              # both
```

`valid: false` means the envelope describes a different workload shape
than the class name claims (e.g. "4096 in / 512 out" that really
emitted 110 tokens) -- `run.py` warns and `gateway_diff.py` raises
`workload_shape_invalid`. Tolerances: `workload_validation_tolerance_pct`
(input, default 10) and `output_validation_tolerance_pct` (default 25 --
models legitimately stop a little early).

## SLO profiles

SLOs live in `constraints/slo.yaml` -- one definition shared by every
experiment and model. TTFT can be shared, but a 512-token generation
can't be held to the same end-to-end budget as a 64-token reply, so
there are named profiles and a default:

```yaml
# constraints/slo.yaml
default: interactive
profiles:
  interactive:     {ttft_p95_ms: 1000, latency_p95_ms: 3000,  success_rate_min: 0.99, throttle_rate_max: 0.001}
  long_generation: {ttft_p95_ms: 1000, latency_p95_ms: 10000, success_rate_min: 0.99, throttle_rate_max: 0.001}
```

An experiment's workloads only NAME a profile (or get the default):

```yaml
workloads:
  - {name: short_short, input_tokens: 512,  output_tokens: 64}
  - {name: long_long,   input_tokens: 4096, output_tokens: 512, slo_profile: long_generation}
```

Isolated workloads are gated on their own profile. In a mix, every
request counts toward goodput against its own class's SLO, every class
is gated on its own profile, and the blend only on the default
profile's success/throttle gates. The answer is therefore model +
workload class + SLO class + quota -> envelope, never one universal
concurrency.

## Mixed workloads

`experiments/mixed-capacity.yaml` sweeps ONE offered rate where each
arrival draws its class by weight (70% short / 30% long_long), so
classes genuinely overlap in flight. A point passes only if the SLO
holds for the blend **and every class** -- a blended p95 can look fine
while the long class alone blows its latency SLO. The artifact gains:

```yaml
mixed_workloads:
  short70_long30:
    shares: {short: 0.7, long_long: 0.3}
    rate: {max_safe_offered_rps: ..., slo_goodput_rps: ..., production_offered_rps: ...}
    evidence: {...}
    classes_at_recommended_point: {short: {...}, long_long: {...}}
```

It's valid for that mix's shares only -- a different traffic mix
needs its own run. Classes measured only inside a mix get
`observed`/`workload_validation`, never an isolated envelope.

## Gateway recommendation diff

```bash
python scripts/gateway_diff.py --gateway-config examples/gateway-limits.example.yaml \
    results/run-all-<timestamp>/*/*-capacity-profile.yaml
```

Compares schema-v3 profiles against a **snapshot** of
`bedrock-runtime-gateway`'s current limits (a YAML you maintain or
export -- see `examples/gateway-limits.example.yaml`) and prints
findings as YAML, warn first; exits 1 on any warn so it can gate a
config review. It only *proposes* a value where a profile field maps
directly onto a gateway knob:

| Gateway knob | Compared against | Finding |
|---|---|---|
| model `rpm_limit` (`gateway-model-quotas-dev`) | tightest `production_offered_rps x 60` across the model's classes and mixes | warn + proposal if above; warn if unset (fails open) |
| tenant `rpm_limit` | same envelope | warn + proposal if one tenant alone exceeds it; info if tenants sum past it |
| `CONCURRENCY_DEFAULT_TENANT_MAX` | tightest `production_max` | warn + proposal if one tenant can exceed it |
| `CONCURRENCY_GLOBAL_MAX` x processes | tightest `production_max` | info only -- it spans all models |

It also surfaces `workload_shape_invalid` (warn) and
`throttle_slo_unresolved` (info). It never reads or writes live gateway
config; the gateway's own review still decides.

## Not in scope here

AIMD, tenant limiters, global admission control, queueing, fairness --
all `bedrock-runtime-gateway`'s job. This repo outputs a safe operating
envelope (`production_max`, `production_offered_rps`) and, at most, an
advisory diff against a gateway config snapshot; it never implements
or applies the runtime logic that enforces it.

## Testing

Python 3.11 or 3.12 (`requires-python` in `pyproject.toml`); `.python-version`
pins 3.11 as the default dev interpreter. CI runs the tests on both
and installs from `pyproject.toml` exactly as below -- no separate
dependency list to drift.

```bash
.venv/bin/python -m pytest -q
```

All runner/metrics/capacity logic is tested against a fake Bedrock
client (`tests/fakes.py`) -- no real network or AWS credentials needed
to run the suite.
