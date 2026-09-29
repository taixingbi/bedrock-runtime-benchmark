# eval-bedrock-runtime-benchmark

Measures the **SLO-qualified operating envelope of a Bedrock inference
profile** -- how much load a model serves within its SLO, statistically
confirmed -- and turns it into an admission-envelope recommendation.

## The paved road

One command at a time (setup: [Install](#install)):

```bash
bedrock-benchmark doctor --model nova-micro
```

```bash
bedrock-benchmark plan capacity-reference-concurrency --model nova-micro
```

```bash
bedrock-benchmark pilot capacity-reference-concurrency --model nova-micro
```

```bash
caffeinate -i bedrock-benchmark run capacity-reference-concurrency --model nova-micro --ticket CAP-123 --purpose "model onboarding"
```

```bash
bedrock-benchmark summary results/run-all-<timestamp>
```

```bash
bedrock-benchmark validate results/
```

```bash
bedrock-benchmark publish results/run-all-<timestamp> --destination s3://<team-bucket>/capacity
```

| Step | Answers | AWS calls |
|---|---|---|
| `doctor` | Is this machine / account / model / config ready? Python, boto3, git clean, config valid, identity + region, quota file = live, model access, ConverseStream, token-counting strategy -> `READY` / `NOT READY -- fix: ...` | 2-3 one-token requests |
| `plan` | What will run, against which quota and ceilings, for how long? | STS only |
| `pilot` | Does each workload reach its shape and SLO at all? | a few requests |
| `run` | The benchmark -> `capacity-profile.yaml` + raw JSONL, then a human summary | the full run |
| `summary` | Can I trust this run? What did we learn? Is it production usable (never, alone)? What next? | none |
| `validate` | Do repeated runs agree across days / times? -> `temporal-capacity-profile.yaml` with a status per envelope: `VALID`, `VALID_CONSERVATIVE`, `INSUFFICIENT_EVIDENCE` | none |
| `publish` | Copies the run to the team's shared, immutable location with a `manifest.yaml` (owner, ticket, sha256 per file) | S3 (or a directory) |
| `validate-profile` | Does an artifact conform to the machine contract ([schemas](src/bedrock_benchmark/schemas/))? | none |

Rules the CLI enforces:

- **No broad expensive defaults.** `plan` / `pilot` / `run` need `--model`
  (repeatable) or an explicit `--all-models`.
- **Every run has an owner.** `run` records a `run:` block in the profile:
  `run_id`, `owner` (default `$USER`), `purpose`, `ticket`, `environment`
  (default `dev`).
- **A single run is never production config.** Only a
  temporal-capacity-profile entry with status `VALID` or
  `VALID_CONSERVATIVE` carries a `production_capacity_input`.

## Who uses what

| Role | Uses | Owns |
|---|---|---|
| Benchmark maintainer | everything; `docs/`, `tests/` | methodology, statistics, experiment YAMLs, the artifact schemas |
| Model onboarding engineer | `doctor` -> `plan` -> `pilot` -> `run` -> `summary` -> `publish` | `catalog/models.yaml`, `constraints/quota.yaml` entries for the new model |
| Platform engineer | published `temporal-capacity-profile.yaml` (`production_capacity_input`) | turning a VALID envelope into capacity planning |
| Gateway engineer | `capacity-profile.yaml` via `eval-bedrock-gateway`'s `capacity_review`, validated with `validate-profile` | the gateway's admission config -- never this repo |
| SRE | `summary`, `validate`, the publish manifest | deciding when a profile is stale and re-measuring |
| Service / product owner | -- | `constraints/slo.yaml` (gold / silver / bronze): SLOs are policy inputs; the benchmark never tunes or relaxes them |

Four layers, each usable without the one below it:

| Layer | Interface | For |
|---|---|---|
| CLI | `bedrock-benchmark ...` | everyone |
| Engine | `bedrock_benchmark.batch.run_batch`, `pilot`, `drift.build_temporal_profile`, `doctor.run_doctor` | automation |
| Artifact API | `capacity-profile.yaml` (schema v23), `temporal-capacity-profile.yaml` (v1), JSON Schemas in `src/bedrock_benchmark/schemas/` | consumers |
| Consumers | `eval-bedrock-gateway` (`capacity_review`), `eval-bedrock-platform` | policy and deployed validation |

## What problem does this solve?

A gateway in front of Bedrock needs limits -- how many requests in
flight, how many per second -- per model and per kind of request.
Guessing them either wastes capacity or lets a model fall over. This
repo measures them directly against Bedrock and answers:

> Given workload W, provider environment E, quota Q and SLO S, what
> operating region satisfies S -- observed, statistically confirmed,
> and safe to configure after headroom?

Two framing rules:

- **What is measured is not the bare model** but the Bedrock
  inference-profile operating envelope: model + Bedrock serving stack +
  inference-profile routing + account/region quota + provider
  conditions at measurement time. A result like "6.67 rps, TTFT 700 ms,
  C=4" describes that whole stack at that time -- never a model's
  intrinsic capacity.
- **The SLO is an input, not a finding.** Gold / silver / bronze
  (`constraints/slo.yaml`) are externally supplied policy; the benchmark
  never derives, tunes or relaxes them from measurements.
- **Capacity = the highest statistically confirmed SLO-compliant
  operating point.** SLO goodput is reported as an observed metric but
  never selects it; headroom is policy, applied only in the
  recommendation.

## Architecture

```
catalog/models.yaml          which models
catalog/workloads.yaml       which requests (shape + SLO profile + role) ─┐
experiments/*.yaml           how to load them (purpose + workloads + sweep) ┤
constraints/slo.yaml         SLO: what quality we require (policy)      ─┤
constraints/quota.yaml       quota: what the provider allows            ─┤
constraints/recommendation-policy.yaml   headroom (policy)              ─┤
                                                                          v
   coarse sweep ──> bracket + refinement ──> adaptive confirmation ──> verdicts (PASS / FAIL / INCONCLUSIVE)
                                                                          |
                                                                          v
                capacity-profile.yaml:  measurement  +  recommendation.admission_envelope
                                                                          |
                                          ───────────── contract ─────────┼──────────────
                                                                          v
                         eval-bedrock-gateway  (scripts/capacity_review.py maps the
                         envelope onto its own global / tenant / quota knobs)
```

Every call goes **directly to Bedrock** (`boto3` Converse /
ConverseStream): no API Gateway, auth, admission control, tenant quota
or queue in the path. The producer knows nothing about its consumers --
there is no gateway config schema in this repo.

| Repo | Question |
|---|---|
| `eval-bedrock-gateway` | Is the gateway's own implementation correct? How does it map an envelope onto its limits? |
| `eval-bedrock-platform` | Does the *deployed platform* (gateway + Bedrock) behave correctly under real workload? |
| `eval-bedrock-runtime-benchmark` | What is the Bedrock inference-profile operating envelope, independent of any gateway? |

## Experiments

| Experiment | Purpose | Sweep | Per model |
|---|---|---|---|
| `capacity-reference-rate` | reference | 0.25x-2.5x of the provider ceiling, one reference workload per tier (`short_chat` gold, `rag_answer` silver, `long_generation` bronze) -- the canonical envelope run | ~57 min |
| `capacity-reference-concurrency` | reference | each reference workload alone, concurrency 1..48 until 2 consecutive FAILs; confirms the top 2 non-failing concurrencies | ~1.5 h |
| `capacity-mix-rate` | reference | mixed-rate calibration: 0.25x-2.5x ceiling for a workflow mix (`--mix`); default 60/30/10 is a reference example | ~32 min |
| `capacity-shape-concurrency` | admission_calibration | the 4 non-reference shapes plus a long_generation reference control x concurrency 1..48 until 2 consecutive FAILs, each under its own SLO | ~1-2 h |

Only **reference** experiments, on the three **reference** workloads (one
per SLO tier), produce an admission-envelope recommendation.
`capacity-shape-concurrency` is **admission calibration**: for the other four catalog
shapes it produces statistically confirmed `calibration_point`s --
C_safe = f(input/output tokens, SLO, quota, provider conditions):

```
calibration_point -> workload-specific admission evidence -> policy derivation (gateway) -> mixed validation (eval-bedrock-platform)
```

A calibration point is evidence, not a config value: no admission
envelope, no headroom. Different workload shapes may require different concurrency to reach the same provider rate ceiling; therefore concurrency must not be interpreted as a workload cost weight. Rates are kept apart -- `attempted_rps` (inflated by fast 429s under overload), `successful_rps` (served), `throttled_rps`, `slo_goodput_rps` -- plus `ceiling_ratio` (served / nominal ceiling); all are observations of what concurrency and latency produced, not a tested rate like `capacity-reference-rate`'s `sustained_rps`. Calibration points are isolated-workload measurements; per-shape C_safe values don't combine mathematically into a global policy, and any policy derived from them must be validated under representative mixed traffic through the deployed gateway (`eval-bedrock-platform`) before production use -- this repo calls Bedrock directly and never validates gateway policy.

Each reference workload ends up with both an isolated concurrency and an
isolated rate envelope:

| Class | `max_inflight` (`capacity-reference-concurrency`) | `sustained_rps` (`capacity-reference-rate`) |
|---|---|---|
| `short_chat` (gold) | ✓ | ✓ |
| `rag_answer` (silver) | ✓ | ✓ |
| `long_generation` (bronze) | ✓ | ✓ |
| the 60/30/10 mix | -- | ✓ (`capacity-mix-rate`) |

Per-class `max_inflight` (and `sustained_rps`) values are **isolated** limits -- each holds for that class running alone (`scope: isolated_workload_class`). They are not additive across classes and are not a global limit; only `capacity-mix-rate` (`scope: workload_mix`) measures classes together.

What each experiment gives a gateway:

| Experiment | Produces | Gateway use |
|---|---|---|
| `capacity-reference-concurrency` | per-class isolated `max_inflight` for the three reference workloads | reference workload `C_admission` |
| `capacity-reference-rate` | per-class isolated `sustained_rps` for the same three | reference workload `R_admission` |
| `capacity-shape-concurrency` | confirmed `calibration_point` per non-reference workload shape (no envelope, no headroom) | extra workload-shape admission calibration points |
| `capacity-mix-rate` | `sustained_rps` for ONE explicit mix (`scope: workload_mix`) | mix-scoped total-rate `R_admission(mix)` |

None of these validates gateway policy: every call goes straight to
Bedrock. The benchmark produces backend admission evidence; the gateway
derives its config from it; `eval-bedrock-platform` validates the
deployed gateway under production-like mixed traffic.

**Each workflow has its own `R_safe(mix)`.** Define weights from that workflow's
production traffic in `catalog/mixes.yaml`, with `source: production_traffic_profile`
and `observed_from` identifying the data and time range. Measure each workflow separately:

```sh
bedrock-benchmark plan capacity-mix-rate --model nova-micro --mix <workflow-name>
bedrock-benchmark run capacity-mix-rate --model nova-micro --mix <workflow-name>
```

The shipped 60/30/10 mix is a reference example only. Every class must meet its
own SLO before the total rate qualifies as `R_safe(mix)`; recommendation headroom
then produces `R_admission(mix)`. Isolated class capacities cannot be added.

`mix.assignment: stratified` uses shuffled blocks with the configured class
counts (6/3/1 for the reference example); a window may end with a partial block.
Use `stochastic` for independent weighted draws that model traffic randomness.
Stratified mixes require an exact block of at most 10,000 requests; unsupported
weights fail validation instead of silently rounding away rare classes.
Artifacts record `configured_mix` and `observed_mix`, with observed counts and
shares for each sweep point and measured confirmation candidate as well.

Confirmation caps are `auto`: request budget is 1.25 times the last look's
required total; duration is the number of windows needed at the candidate RPS,
including per-window warmup, times 1.25. Each candidate gets its own budget,
so low-RPM models can take hours. Numeric caps still impose explicit limits.

A mixed/global total in-flight limit would need its own experiment
(not built -- add one only if a consumer needs it). `max_inflight` and
`sustained_rps` are two independently confirmed **guardrails**, one per
dimension: the concurrency sweep controls C and lets the rate emerge;
the rate sweep controls R and lets concurrency emerge. Enforcing both
(C <= max_inflight and R <= sustained_rps) is conservative, but it is
not a statistically confirmed 2-D (C, R) capacity surface -- no joint
(C, R) point near the recommendation has been validated (planned:
`joint-capacity`).

**A single run is not a production config.** Every profile is a
`single_run_operating_envelope`; repeat across times and days, and take
`production_capacity_input` from `bedrock-benchmark validate`'s
temporal-capacity-profile -- the conservative value, set only once the
temporal evidence suffices (status `VALID` / `VALID_CONSERVATIVE`).

Every experiment is discovery followed by adaptive confirmation at the
candidate -- the only way a point becomes statistically confirmed.
Times are nova-micro `--dry-run` estimates.

## Install

```bash
python3.11 -m venv .venv && .venv/bin/pip install -e ".[dev]"
```

```bash
source .venv/bin/activate
```

```bash
export AWS_PROFILE=<your-profile> AWS_REGION=us-east-1
```

That installs the `bedrock-benchmark` command (without activating the
venv: `.venv/bin/bedrock-benchmark`). You name models and experiments --
never file paths; `bedrock-benchmark list` shows both. It works from any
directory (it finds the checkout, or set `BEDROCK_BENCHMARK_HOME`).
`run ... --dry-run` is `plan`, `run ... --pilot` is `pilot`; `all` runs
every experiment; `--slo-profile gold` keeps only workloads bound to a
profile. Results go to `results/run-all-<timestamp>/<model>/`.
(`scripts/*.py` still work but are not the public interface.)

## Output example

Per workload class, measurement and recommendation are separate blocks.
Abridged, from the 2026-09-26 nova-micro `capacity-reference-rate` run (full schema:
[docs/capacity-profile-schema.md](docs/capacity-profile-schema.md)):

```yaml
workload_classes:
  short_chat:
    slo_profile: gold
    rate:                                       # MEASUREMENT
      observed_nonfailing_offered_rps: 8.3333
      observed_verdict: INCONCLUSIVE            # no violation seen, too few requests to prove it
      statistically_confirmed_offered_rps: 6.6667
      provider_ceiling_rps: 6.6667
      saturation: {observed_edge: 10.0, phase: discovery, status: discovery_resolved}   # observed, not confirmed
    confirmation:
      candidates: [{value: 6.6667, verdict: PASS, stop_reason: confirmed, n: 4173}]
    recommendation:                             # POLICY
      admission_envelope:
        max_inflight: null
        sustained_rps: 5.3334                   # min(6.6667 x 0.8, 6.6667 x 0.9)
        source: statistically_confirmed_measurement
        headroom_fraction: 0.2
        binding: measurement
```

No statistically confirmed point means `admission_envelope: null`: a
recommendation is never derived from an observed-only or INCONCLUSIVE
point, and `reason` says why -- each candidate's verdict and
`stop_reason` (e.g. `confirmation at 6.6667: FAIL (violation_demonstrated,
n=4380)`).

## Not in scope

Tenant quotas, fairness, queue policy, AIMD, adaptive global
concurrency, auth and gateway config -- all `eval-bedrock-gateway`'s
job. This repo outputs only a safe backend operating envelope (measured,
statistically confirmed, plus an admission-envelope recommendation); it
never implements, applies or pre-decides the runtime policy that
enforces it, and reads nothing from a gateway's tables or config.

## Documentation

| Doc | Covers |
|---|---|
| [methodology](docs/methodology.md) | core abstractions, SLO goodput, measurement window, input calibration and workload validation, mixed workloads, provenance and temporal validation |
| [SLO statistics](docs/slo-statistics.md) | SLO profiles, PASS / FAIL / INCONCLUSIVE, exact bounds for latency / success / throttle, adaptive confirmation, false-PASS and false-FAIL control |
| [quota model](docs/quota-model.md) | provider ceiling, TPM reservation vs consumption, quota-relative sweeps, `fetch_quota.py` |
| [capacity-profile schema](docs/capacity-profile-schema.md) | the deliverable and its consumer contract, measurement vs recommendation; the temporal-capacity-profile and the machine-readable JSON Schemas |
| [experiment design](docs/experiment-design.md) | models, workload catalog, experiments, constraints, running, pilot |
| [admission control](docs/admission-control.md) | minimum gateway admission config, two guardrails (not a 2-D region), production capacity input |
| [benchmark outputs](docs/benchmark-outputs.md) | every output, from admission values to evidence and temporal validation |
| [correctness history](docs/correctness-history.md) | every measurement bug found in review, by schema version |

## Testing

```bash
.venv/bin/python -m pytest -q
```

Python 3.11 or 3.12 (`requires-python` in `pyproject.toml`);
`.python-version` pins 3.11. All runner, metrics, statistics and
recommendation logic is tested against a fake Bedrock client
(`tests/fakes.py`) -- no network or AWS credentials needed.

`capacity-shape-concurrency` also runs `long_generation` (4096 input / 1024 output, bronze)
as an experiment-local `reference_control`. Its measurements help compare decode
pressure with large-context behavior; it emits no calibration point or production
admission recommendation in this experiment.

### Follow-up experiments for provider-state behavior

`diagnostic-context-history` compares `long_context_short_answer` and `very_large_context`
at 0.25 of each workload's nominal rate ceiling (1.67 RPS for the recorded
400-RPM configuration). Every arm starts with 300s idle and a healthy low-load
probe. The overload arm then applies 2x nominal rate for 120s, waits 120s and
requires another healthy probe. Each observation is one continuous 900s window;
30s bins do not interrupt traffic. Two trials reverse arm order and use paired
arrival seeds. Idle and healthy probes do not establish that the provider reset.
Record any other traffic sharing the quota. A configured overload need not cause
throttling: the artifact records whether it did. This is descriptive evidence,
not a capacity recommendation or proof of a provider-internal mechanism.

Bins report offered/scheduled/attempted/successful/throttled RPS, request-cohort
success/throttle rates, TTFT, latency, peak inflight, and scheduling lag. Rate
counts use scheduled arrivals, actual starts, or completions as named; the
success/throttle proportions follow requests scheduled in the bin, including
responses that finish later. The final bin may therefore show fewer completions
than eventual successes. Raw JSONL keeps measurement, overload and probe phases.

`capacity-shape-concurrency` supports a focused retest using `--workload`,
`--candidate-concurrency`, and `--steady-state-duration-s` together. For example, retest C=7 using a continuous 30-minute discovery window
and fresh confirmation with at least 30 minutes of measured exposure. Confirmation
may reject the candidate or require additional windows. Its measured RPS is not a
validated rate envelope. Compare repeated runs before deriving an admission policy.

```sh
.venv/bin/bedrock-benchmark plan diagnostic-context-history --model nova-micro
.venv/bin/bedrock-benchmark run diagnostic-context-history --model nova-micro
.venv/bin/bedrock-benchmark run capacity-shape-concurrency --model nova-micro \
  --workload medium_context --candidate-concurrency 7 --steady-state-duration-s 1800
```

History comparison takes about three hours before recovery retries/drain; the
sustain retest typically needs at least an hour if its candidate remains eligible.
Run them separately to avoid contaminating their provider state with each other.
Reports retain `nominal_binding_constraint`, but throttling is described as an
observed symptom. Suspect measurements use `bottleneck: unresolved`; public quota
ratios alone do not establish the actual cause of provider rejection.

Focused retests keep the base experiment name and set `mode: sustain`; regular
capacity sweeps use `mode: sweep`, and history diagnostics use `mode: history`.
Retest parameters are recorded under `measurement.retest`. Temporal comparison
groups by experiment, model, workload/mix, mode, candidate concurrency and
measurement duration (including minimum confirmation exposure). Historical
experiment names remain distinct; result files are never renamed or rewritten.
The retest options work for plan, pilot and run. Without them the original shape
sweep applies.

### Experiment names

| Previous name | Current name |
|---|---|
| `concurrency-sweep` | `capacity-reference-concurrency` |
| `rate-capacity` | `capacity-reference-rate` |
| `mixed-capacity` | `capacity-mix-rate` |
| `workload-shape-calibration` | `capacity-shape-concurrency` |
| `long-context-history` | `diagnostic-context-history` |

Use the current names in commands. `purpose` defines how results can be used;
workload roles such as `reference_control` remain explicit configuration fields.

### Streaming measurements

All five experiments use a shared descriptive metrics structure with latency
p50/p95/p99 and sample counts, request/token throughput, reliability and load state.
Capacity reports expose each repetition under `measurement_windows`, with 30-second
bins and per-class breakdowns for mixed traffic. History reports use
`history_comparison[].aggregate.metrics` and `bins[].metrics`.

Raw JSONL also preserves submission and last-text timing, including partial-stream
failures. Existing TPOT SLO checks keep their definition; the additional
`text_decode_tpot_ms` excludes trailing metadata time and is descriptive only.
See [metric definitions](docs/capacity-profile-schema.md#common-descriptive-metrics-metrics_version-1).
