# eval-bedrock-runtime-benchmark

Empirically characterizes the **SLO-qualified operating envelope of a
Bedrock inference profile** under controlled token workloads and
provider constraints, and turns the statistically confirmed part of it
into an admission-envelope recommendation.

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
| `rate-capacity` | reference | 0.25x-2.5x of the provider ceiling, one reference workload per tier (`short_chat` gold, `rag_answer` silver, `long_generation` bronze) -- the canonical envelope run | ~57 min |
| `concurrency-sweep` | reference | each reference workload alone, concurrency 1..48 until 2 consecutive FAILs; confirms the top 2 non-failing concurrencies | ~1.5 h |
| `mixed-capacity` | reference | mixed-rate calibration: 0.25x-2.5x ceiling for ONE mix, 60% `short_chat` / 30% `rag_answer` / 10% `long_generation` | ~32 min |
| `workload-shape-calibration` | admission_calibration | the 4 non-reference shapes x concurrency 1..48 until 2 consecutive FAILs, each under its own SLO | ~1-2 h |

Only **reference** experiments, on the three **reference** workloads (one
per SLO tier), produce an admission-envelope recommendation.
`workload-shape-calibration` is **admission calibration**: for the other four catalog
shapes it produces statistically confirmed `calibration_point`s --
C_safe = f(input/output tokens, SLO, quota, provider conditions):

```
calibration_point -> workload-specific admission evidence -> policy derivation (gateway) -> mixed validation (eval-bedrock-platform)
```

A calibration point is evidence, not a config value: no admission
envelope, no headroom. Different workload shapes may require different concurrency to reach the same provider rate ceiling; therefore concurrency must not be interpreted as a workload cost weight. Rates are kept apart -- `attempted_rps` (inflated by fast 429s under overload), `successful_rps` (served), `throttled_rps`, `slo_goodput_rps` -- plus `ceiling_ratio` (served / nominal ceiling); all are observations of what concurrency and latency produced, not a tested rate like `rate-capacity`'s `sustained_rps`. Calibration points are isolated-workload measurements; per-shape C_safe values don't combine mathematically into a global policy, and any policy derived from them must be validated under representative mixed traffic through the deployed gateway (`eval-bedrock-platform`) before production use -- this repo calls Bedrock directly and never validates gateway policy.

Each reference workload ends up with both an isolated concurrency and an
isolated rate envelope:

| Class | `max_inflight` (`concurrency-sweep`) | `sustained_rps` (`rate-capacity`) |
|---|---|---|
| `short_chat` (gold) | ✓ | ✓ |
| `rag_answer` (silver) | ✓ | ✓ |
| `long_generation` (bronze) | ✓ | ✓ |
| the 60/30/10 mix | -- | ✓ (`mixed-capacity`) |

Per-class `max_inflight` (and `sustained_rps`) values are **isolated** limits -- each holds for that class running alone (`scope: isolated_workload_class`). They are not additive across classes and are not a global limit; only `mixed-capacity` (`scope: workload_mix`) measures classes together.

What each experiment gives a gateway:

| Experiment | Produces | Gateway use |
|---|---|---|
| `concurrency-sweep` | per-class isolated `max_inflight` for the three reference workloads | reference workload `C_admission` |
| `rate-capacity` | per-class isolated `sustained_rps` for the same three | reference workload `R_admission` |
| `workload-shape-calibration` | confirmed `calibration_point` per non-reference workload shape (no envelope, no headroom) | extra workload-shape admission calibration points |
| `mixed-capacity` | `sustained_rps` for ONE explicit mix (`scope: workload_mix`) | mix-scoped total-rate `R_admission(mix)` |

None of these validates gateway policy: every call goes straight to
Bedrock. The benchmark produces backend admission evidence; the gateway
derives its config from it; `eval-bedrock-platform` validates the
deployed gateway under production-like mixed traffic.

**One mix is one number.** `mixed-capacity`'s 60/30/10 gives
R_safe(60/30/10), not a global R_safe -- and after headroom,
R_admission(60/30/10). A chat-heavy, balanced or generation-heavy mix
can need a very different R_admission. For a shifting production mix,
measure the representative mixes and take the minimum across them as a
conservative limit, or configure per known traffic profile. Each mix is
its own experiment file (e.g. `mixed-capacity-chat-heavy.yaml`) -- one
`mix:` per file keeps confirmation, sample sizes and the report simple;
only 60/30/10 is shipped.
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
`scripts/drift.py`'s `temporal_validation.production_capacity_input` --
the conservative value, set only once the temporal evidence suffices.

Every experiment is discovery followed by adaptive confirmation at the
candidate -- the only way a point becomes statistically confirmed.
Times are nova-micro `--dry-run` estimates.

## Quick start

```bash
python3.11 -m venv .venv && .venv/bin/pip install -e ".[dev]"
source .venv/bin/activate
export AWS_PROFILE=<your-profile> AWS_REGION=us-east-1
```

That installs the `bedrock-benchmark` command. You only name a model and
an experiment -- no file paths (one command at a time):

```bash
bedrock-benchmark list
```

```bash
bedrock-benchmark plan workload-shape-calibration --model nova-micro
```

```bash
bedrock-benchmark pilot workload-shape-calibration --model nova-micro
```

```bash
caffeinate -i bedrock-benchmark run workload-shape-calibration --model nova-micro
```

| Command | Does | AWS calls |
|---|---|---|
| `list` | models and experiments | none |
| `plan <experiment> --model <m>` | validates config + quota, shows workloads, ceilings and estimated runtime | STS only (which account) |
| `pilot <experiment> --model <m>` | a few requests per workload: access, workload shape, SLO reachability, throttling | a few |
| `run <experiment> --model <m>` | the benchmark -> `capacity-profile.yaml` + raw JSONL | the full run |

`run ... --dry-run` is `plan`, `run ... --pilot` is `pilot`. `all` runs
every experiment; `--model` repeats (default: every enabled model);
`--slo-profile gold` keeps only workloads bound to a profile. It works
from any directory (it finds the checkout, or set
`BEDROCK_BENCHMARK_HOME`). Check quotas are current first with
`.venv/bin/python scripts/fetch_quota.py --all`. Results go to
`results/run-all-<timestamp>/<model>/`. (`scripts/run.py` and
`scripts/run_all.py` still work but are not the public interface.) Each profile is a
**single-run operating envelope**. Repeated runs on different days and
times of day combine into a stable / conservative envelope
(`temporal_validation`) with:

```bash
.venv/bin/python scripts/drift.py results/
```

## Output example

Per workload class, measurement and recommendation are separate blocks.
Abridged, from the 2026-09-26 nova-micro `rate-capacity` run (full schema:
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
point, and `reason` says why (e.g. `confirmation at 6.6667: FAIL
(observed_violation, n=620)` for silver `rag_answer`).

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
| [SLO statistics](docs/slo-statistics.md) | SLO profiles, PASS / FAIL / INCONCLUSIVE, exact bounds for latency / success / throttle, adaptive confirmation and false-PASS control |
| [quota model](docs/quota-model.md) | provider ceiling, TPM reservation vs consumption, quota-relative sweeps, `fetch_quota.py` |
| [capacity-profile schema](docs/capacity-profile-schema.md) | the deliverable and its consumer contract, measurement vs recommendation |
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
