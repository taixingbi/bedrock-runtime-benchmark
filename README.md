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
checked-in fixtures). The `capacity-profile.yaml` schema:

```yaml
schema_version: 1
model: {provider: bedrock, model_id: ..., region: ...}
quota: {rpm: ..., tpm: ...}
slo: {ttft_p95_ms: ..., latency_p95_ms: ...}
profiles:
  short: {input_tokens: 512, output_tokens: 64, sustainable_rps: 5.1,
          recommended_concurrency: 6, saturation_concurrency: 8}
recommendation:
  provider_headroom: 0.20
  gateway:
    global_min_concurrency: 1
    global_max_concurrency: 6
    classes: {short: {max_concurrency: 4}}   # headroom-adjusted
```

This is the actual deliverable -- not an HTML report. A gateway's own
config review reads this file.

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
