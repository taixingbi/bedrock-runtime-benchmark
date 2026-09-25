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
  this; see `token-sweep.yaml`. Input is padded at ~4 chars per token.
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

## Running an experiment

```bash
python3.11 -m venv .venv && .venv/bin/pip install -e ".[dev]"   # same install CI uses
.venv/bin/python scripts/run.py experiments/concurrency-sweep.yaml
```

Writes raw per-request JSONL and a `capacity-profile.yaml` artifact to
`results/` (gitignored -- these are real measurement outputs, not
checked-in fixtures). The `capacity-profile.yaml` schema (v3 -- see
"Correctness fixes" below for why `rate` and `concurrency` are always
kept in separate blocks, why the rate block separates offered load
from goodput, and why there's no `global_max_concurrency`):

```yaml
schema_version: 3
model: {provider: bedrock, model_id: ..., region: ...}
quota_snapshot: {rpm: 400, tpm: 8000000}
slo: {ttft_p95_ms: 1000, latency_p95_ms: 3000, success_rate_min: 0.99, throttle_rate_max: 0.001, confidence: null}
measurement:
  warmup_s: 10
  window_s: 90
  repetitions: 1
  window_policy: scheduled_in_window_for_rates__completed_in_window_for_throughput
  gate: point_estimate                      # or confidence_bound when slo.confidence is set
  min_requests_to_resolve_throttle_slo: 2703
workload_classes:
  short:                                    # a concurrency-sweep result
    observed: {input_tokens_p50: 505, output_tokens_p50: 61}
    concurrency: {measured_best: 6, saturation: 8, production_max: 4, slo_goodput_rps: 5.1}
    evidence: {n: 612, n_throttled: 0, throttle_rate: 0.0, throttle_rate_upper: 0.0044, ...}
  short_rate:                               # a rate-sweep result (same workload, different sweep type)
    observed: {input_tokens_p50: 505, output_tokens_p50: 61}
    rate:
      max_safe_offered_rps: 6.0             # the swept offered rate that passed the SLO
      slo_goodput_rps: 5.8                  # what it actually delivered within SLO
      saturation_offered_rps: 7.0
      production_offered_rps: 4.8           # headroom applied to OFFERED rate -- what a gateway limit reads
    evidence: {...}
provider: {headroom: 0.20}
transport: {max_connections: 64, total_max_attempts: 1, connect_timeout_s: 5, read_timeout_s: 60}
```

This is the actual deliverable -- not an HTML report. A gateway's own
config review reads this file, and decides its own global/tenant/AIMD
config FROM these per-class envelopes -- this repo never pre-packages
a gateway control policy itself (see "Not in scope here" below).

## Correctness fixes (schema v2 / v3)

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

## Measurement policy (schema v3)

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

## The three MVP experiments

- **`concurrency-sweep.yaml`** -- one workload, concurrency 1/2/4/6/8.
  The fast, everyday one (~10 min).
- **`token-sweep.yaml`** -- the 2x2 input/output token grid (short-short
  / output-heavy / input-heavy / long-long), each concurrency-swept.
  Slow (~24 min); run occasionally, not on every change.
- **`slo-capacity.yaml`** -- a *rate* sweep (not concurrency) straddling
  the model's real RPM ceiling, to separate latency saturation from
  RPM/throttling saturation. The canonical "give me the production
  envelope" run.

## Quota-aware experiment design

Picking sane sweep values (especially rate-sweep values) is guesswork
without knowing the model's real RPM/TPM ceiling first -- a
concurrency sweep starting at `[1, 2, 4, ...]` is useless if even
concurrency=1 closed-loop already runs over quota, which turns out to
be true for some certified models. `scripts/fetch_quota.py` and
`src/bedrock_benchmark/quota.py` exist to make that ceiling a known
number before an experiment is written, not a name for it to just do.

```bash
.venv/bin/python scripts/fetch_quota.py --model-id us.amazon.nova-pro-v1:0
```

prints a `quota_snapshot:` block ready to paste into an experiment
file. It looks up the model's real RPM/TPM in two steps, table first:

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

Two experiments were designed this way, from real quota numbers plus
a live single-request latency check at each model's real ceiling:

- **`nova-pro-rate-capacity.yaml`** -- nova-pro's real quota (50 RPM
  = 0.83 rps) is far tighter than nova-micro/lite's, and its ~616ms
  real per-call latency means concurrency=1 closed-loop already runs
  at ~1.6 rps -- about 2x over quota before a concurrency sweep would
  even start sweeping. Only a fractional-rps rate sweep (`[0.2, 0.4,
  0.6, 0.8, 1.2, 1.6, 2.0]`) can resolve where its safe zone actually
  is.
- **`llama3-70b-rate-capacity.yaml`** -- llama3-3-70b's real quota (80
  RPM = 1.33 rps) sits almost exactly at its own concurrency=1
  closed-loop rate (~1.32 rps, from a ~759ms real per-call latency),
  and this model has the tightest TPM budget relative to RPM (600,000
  TPM) of any certified model -- worth watching for whether TPM or
  RPM saturates first. Same fractional-rps rate-sweep approach,
  bracketing the ceiling from `[0.4 .. 3.0]`.

## Workload validation

Prompts are padded at a fixed ~4 chars ≈ 1 token (the same estimate
`bedrock-runtime-gateway` uses). Tokenizers differ per model, so the
estimate can miss; each class's `workload_validation` compares the
requested input tokens with the p50 Bedrock actually *reported* during
the run:

```yaml
workload_validation:
  requested_input_tokens: 512
  padding: 4_chars_per_token_estimate
  observed_input_tokens_p50: 507
  deviation_pct: -0.98
  tolerance_pct: 10.0
  valid: true
```

`valid: false` means the envelope describes a different workload shape
than the class name claims -- `run.py` warns and `gateway_diff.py`
raises `workload_shape_invalid`. Tolerance is
`workload_validation_tolerance_pct` (default 10).

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
    results/*-capacity-profile.yaml
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
