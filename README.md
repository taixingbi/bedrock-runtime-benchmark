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
  this; see `token-sweep.yaml`.
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
poetry install   # or: python3 -m venv .venv && .venv/bin/pip install boto3 pyyaml pytest
PYTHONPATH=src python scripts/run.py experiments/concurrency-sweep.yaml
```

Writes raw per-request JSONL and a `capacity-profile.yaml` artifact to
`results/` (gitignored -- these are real measurement outputs, not
checked-in fixtures). The `capacity-profile.yaml` schema (v2 -- see
"Correctness fixes" below for why `rate` and `concurrency` are always
kept in separate blocks, and why there's no `global_max_concurrency`):

```yaml
schema_version: 2
model: {provider: bedrock, model_id: ..., region: ...}
quota_snapshot: {rpm: 400, tpm: 8000000}
slo: {ttft_p95_ms: 1000, latency_p95_ms: 3000, success_rate_min: 0.99, throttle_rate_max: 0.001}
workload_classes:
  short:                                    # a concurrency-sweep result
    observed: {input_tokens_p50: 505, output_tokens_p50: 61}
    concurrency: {measured_best: 6, saturation: 8, production_max: 4}
  short_rate:                               # a rate-sweep result (same workload, different sweep type)
    observed: {input_tokens_p50: 505, output_tokens_p50: 61}
    rate: {measured_sustainable_rps: 5.8, saturation_rps: 7.0, production_rps: 4.64}
provider: {headroom: 0.20}
transport: {max_connections: 64, retry_max_attempts: 1, connect_timeout_s: 5, read_timeout_s: 60}
```

This is the actual deliverable -- not an HTML report. A gateway's own
config review reads this file, and decides its own global/tenant/AIMD
config FROM these per-class envelopes -- this repo never pre-packages
a gateway control policy itself (see "Not in scope here" below).

## Correctness fixes (schema v2)

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
   timeouts, and `retry_max_attempts=1` (no SDK retries) explicitly,
   and records the config used into the artifact for reproducibility.
5. **`global_max_concurrency = max(concurrencies)` across independently
   swept workload classes was invalid.** There's no scientifically
   defensible "global" number derivable from isolated per-class
   maxima -- a real MIXED workload can exceed safe capacity before
   either class's own isolated measurement would predict. Removed
   entirely; a real mixed-workload experiment (not yet built) is the
   only valid way to answer that question.

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
PYTHONPATH=src .venv/bin/python scripts/fetch_quota.py --model-id us.amazon.nova-pro-v1:0
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

## Not in scope here

AIMD, tenant limiters, global admission control, queueing, fairness --
all `bedrock-runtime-gateway`'s job. This repo outputs a safe operating
envelope (`recommended_concurrency`, `sustainable_rps`); it never
implements the runtime logic that enforces it.

## Testing

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/ -q
```

All runner/metrics/capacity logic is tested against a fake Bedrock
client (`tests/fakes.py`) -- no real network or AWS credentials needed
to run the suite.
