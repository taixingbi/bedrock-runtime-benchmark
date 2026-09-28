# Experiment design

> Docs: [methodology](methodology.md) · [SLO statistics](slo-statistics.md) · [quota model](quota-model.md) · [capacity-profile schema](capacity-profile-schema.md) · [experiment design](experiment-design.md) · [correctness history](correctness-history.md) · [README](../README.md)

The inputs a run combines -- models, the workload catalog, experiments and constraints -- and how to run, filter and smoke-test them.

## Models, experiments and constraints

Independent inputs, combined at run time:

```
catalog/                   WHAT exists -- the benchmark's inputs
  ├─ models.yaml           which models: name (= results folder), model_id, region
  └─ workloads.yaml        which requests: shapes, each bound to an SLO profile and a role
constraints/               what every result is JUDGED AGAINST, and the policy applied after
  ├─ slo.yaml              SLO:    what quality we REQUIRE  (gold / silver / bronze)
  ├─ quota.yaml            quota:  what the provider ALLOWS (per account / region / model)
  └─ recommendation-policy.yaml  policy: headroom from a confirmed point to a recommendation
experiments/*.yaml         HOW to load: purpose + workload names + sweep -- no shapes, SLO, quota or headroom
scripts/                   legacy entry points (run.py, run_all.py, fetch_quota.py, drift.py); the public one is bedrock-benchmark
        ↓
every experiment x every model -> capacity-profile.yaml (judged against the constraints)
```

- **Models** -- `catalog/models.yaml`: the five models
  `eval-bedrock-gateway` certifies (nova-micro, nova-lite, nova-pro,
  llama3-3-70b, qwen3-32b). `enabled: false` skips one by default.
- **Experiments** -- `experiments/*.yaml`: model-agnostic workload +
  sweep definitions, each with an explicit `purpose` (see
  [Experiment purposes](#experiment-purposes)).
  Every experiment runs unchanged against every model. They define only
  what is measured: headroom (`provider_headroom` / `quota_headroom`)
  in an experiment is rejected.
- **Constraints** -- `constraints/`: each number defined exactly once.
  The loaders reject SLO or quota numbers anywhere else (an experiment
  with `slo:`/`target:`/`quota:`, a models entry with `quota:`), so no
  copy can drift and every run of the same workload class is judged
  the same way. `--slo-file` / `--quota-file` point a run at other
  constraint files (e.g. a stricter SLO).
  - `slo.yaml`: three service classes (`gold`, `silver`, `bronze`) by
    business criticality of the request, not by model; **no default** --
    each workload in the catalog binds its profile explicitly.
  - `recommendation-policy.yaml`: `headroom_fraction` (0.20) and
    `quota_headroom_fraction` (0.10) -- the POLICY that turns a
    statistically confirmed measurement into a recommendation; never
    applied inside a measurement block.
  - `quota.yaml`: scoped like Bedrock quotas themselves --
    `accounts: {<account id>: {<region>: {<model name>: {rpm, tpm}}}}`.
    The account comes from the live credentials (STS; `--account`
    overrides) and the region from each model's entry; a run on an
    account with no quotas listed fails rather than sweeping around
    another account's numbers. Offline (no credentials), a file with
    exactly one account is used as-is.


| Experiment | Purpose | Sweep | Per model |
|---|---|---|---|
| `rate-capacity.yaml` | reference | 0.25x-2.5x ceiling, one reference workload per tier (`short_chat` gold, `rag_answer` silver, `long_generation` bronze) -- the canonical production-envelope run | ~57 min |
| `concurrency-sweep.yaml` | reference | each of `short_chat` / `rag_answer` / `long_generation` alone, concurrency 1..48 until 2 consecutive FAILs; confirms the top 2 non-failing concurrencies (highest first, e.g. C=4 then C=2) | ~1.5 h |
| `mixed-capacity.yaml` | reference | mixed-rate calibration: 0.25x-2.5x ceiling for ONE mix, 60% `short_chat` / 30% `rag_answer` / 10% `long_generation` | ~32 min |
| `workload-shape-calibration.yaml` | admission_calibration | the 4 non-reference shapes x concurrency 1..48 until 2 consecutive FAILs, each under its own SLO | ~1-2 h |

Every experiment runs adaptive confirmation at its candidate after
discovery (see [SLO statistics](slo-statistics.md)) -- without it one
window per point can't resolve gold's 0.1% throttle limit and nothing
is ever confirmed. Times are `--dry-run` estimates for nova-micro,
assuming each candidate PASSes at the first look; a stray bad event can
push a candidate up to its caps.

Keep `constraints/quota.yaml` current with `scripts/fetch_quota.py --all` (see
[quota model](quota-model.md)) -- a stale quota shifts every
rate a quota-relative sweep tests.

## Workload catalog

Every request shape is defined once in `catalog/workloads.yaml` and
bound there -- explicitly, no default -- to an SLO profile and a role.
Experiments only list workload names; a shape or SLO inside an
experiment is rejected.

```yaml
# catalog/workloads.yaml
workloads:
  # Reference workloads -- one per SLO tier
  short_chat:                {input_tokens: 512,   output_tokens: 64,   slo_profile: gold,   latency_p95_ms: 3000,  role: reference}
  rag_answer:                {input_tokens: 4096,  output_tokens: 256,  slo_profile: silver, latency_p95_ms: 10000, role: reference}
  long_generation:           {input_tokens: 4096,  output_tokens: 1024, slo_profile: bronze, latency_p95_ms: 60000, role: reference}
  # Characterization workloads -- token / context effects
  tiny_request:              {input_tokens: 256,   output_tokens: 32,   slo_profile: gold,   latency_p95_ms: 2000,  role: characterization}
  medium_context:            {input_tokens: 2048,  output_tokens: 128,  slo_profile: silver, latency_p95_ms: 6000,  role: characterization}
  long_context_short_answer: {input_tokens: 8192,  output_tokens: 64,   slo_profile: silver, latency_p95_ms: 8000,  role: characterization}
  very_large_context:        {input_tokens: 16384, output_tokens: 256,  slo_profile: bronze, latency_p95_ms: 20000, role: characterization}

# experiments/workload-shape-calibration.yaml -- the four non-reference shapes
workloads: [tiny_request, medium_context, long_context_short_answer, very_large_context]
```

The catalog is deliberately broader than any experiment: adding a
workload sends no traffic, only experiments that list it do.
`long_context_short_answer` isolates input-side (prefill) pressure and
`long_generation` output-side (decode) pressure; the former is also
TPM-bound on low-TPM models (llama3-3-70b: ~1.21 rps by TPM vs 1.33 by
RPM).

## Experiment purposes

Not every catalog workload gets a capacity recommendation. Workloads
carry a `role` (`reference` for `short_chat` / `rag_answer` /
`long_generation`, one per SLO tier; `characterization` for the other
four), and every experiment declares a `purpose`:

| `purpose` | Experiments | Workloads | Profile carries | Needs `confirmation:` |
|---|---|---|---|---|
| **reference** | `rate-capacity`, `concurrency-sweep`, `mixed-capacity` | reference only (enforced) | measurement **and** `recommendation.admission_envelope` | yes |
| **admission_calibration** | `workload-shape-calibration` | any | measurement **and** a confirmed `calibration_point` per shape; `admission_envelope: null` | yes |
| **characterization** | (none shipped) | any | measurement only | no |

**admission_calibration** answers the gateway question "what safe
concurrency does this workload SHAPE have under the SLO it will be held
to?" -- C_safe = f(input/output tokens, SLO, quota, provider
conditions): a confirmed point for this shape under its SLO, the quota
and the measured provider environment. Each shape keeps its
own business SLO (`tiny_request` gold, `medium_context` and
`long_context_short_answer` silver, `very_large_context` bronze), not a
fixed research SLO. Its `calibration_point` (workload shape, SLO,
`statistically_confirmed_concurrency`, `confirmed_rates`,
`ceiling_ratio`, saturation edge, bottleneck) is workload-specific
admission evidence -- calibration_point -> workload-specific admission evidence -> policy derivation (gateway) -> mixed validation (eval-bedrock-platform) -- never a config value:

```
eval-bedrock-runtime-benchmark (Benchmark -> Bedrock, no gateway in the path)
  concurrency-sweep           ->  C_admission per reference workload       ┐
  rate-capacity               ->  R_admission per reference workload       │  backend admission
  workload-shape-calibration  ->  extra workload-shape calibration points  │  evidence
  mixed-capacity              ->  R_safe for one explicit workload mix     ┘
          |
gateway derives its policy / config from that evidence
          |
eval-bedrock-platform (-> gateway -> Bedrock)
  validates the deployed gateway policy under production-like mixed traffic
```

Rates are kept apart -- `attempted_rps` (inflated by fast 429s under overload), `successful_rps` (served), `throttled_rps`, `slo_goodput_rps` -- plus `ceiling_ratio` (served / nominal ceiling); all are observations of what concurrency and latency produced, not a tested rate like `rate-capacity`'s `sustained_rps`. Different workload shapes may require different concurrency to reach the same provider rate ceiling; therefore concurrency must not be interpreted as a workload cost weight. Calibration points are isolated-workload measurements; per-shape C_safe values don't combine mathematically into a global policy, and any policy derived from them must be validated under representative mixed traffic through the deployed gateway (`eval-bedrock-platform`) before production use -- this repo calls Bedrock directly and never validates gateway policy.

**Next step for shape research (not nova-micro).** On nova-micro every
shape hits the 400 RPM quota first, so this experiment measures
C ~= R_quota x service time. Studying real token-shape capacity needs a
model / quota where RPM doesn't bind first, so each shape can hit its own
limit: long input -> prefill / TPM, long output -> decode / latency,
small requests -> RPM. More nova-micro token shapes won't add to that.

Because it feeds configuration, it needs independent confirmation like
a reference experiment. Both `role` and `purpose` are recorded in the
profile.

Responsibilities, without overlap:

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

Per-class `max_inflight` (and `sustained_rps`) values are **isolated** limits -- each holds for that class running alone (`scope: isolated_workload_class`). They are not additive across classes and are not a global limit; only `mixed-capacity` (`scope: workload_mix`) measures classes together.

**Concurrency ranges.** Concurrency is absolute, but the quota ceiling
is reached at C ~= ceiling_rps x latency (Little's law) -- on nova-micro
~4 for `short_chat`, ~14 for `rag_answer`, ~43 for `long_generation`. So
one wide list serves every class, and `sweep.stop_after_fails: 2` ends a
class's discovery after two consecutive FAILs (saturation seen twice,
enough for the non-monotonic check) instead of sweeping a throttle storm
far past it; the profile lists `sweep_stopped_early.skipped_values`. The
dry-run marks those estimates `<=`: they assume every value runs.
Then `sweep.refinement` bisects the bracket around saturation:

```yaml
refinement:
  strategy: integer_bisection   # the only strategy
  stop_when_adjacent: true      # upper - lower == 1 -> resolved
  max_points: 4                 # resolves a 16-wide gap (32 -> 48: 40, 44, 46, 47)
```

e.g. 8 non-FAIL / 12 FAIL -> 10 -> 11 or 9, so the confirmed candidate
is the real edge rather than the coarse grid point below it (confirmed
11 -> max_inflight 8, vs 8 -> 6). Refinement points appear in
`sweep_points` with `phase: refinement`; they are discovery-class data.

`max_inflight` (`concurrency-sweep`) and `sustained_rps`
(`rate-capacity`) are two independently confirmed guardrails -- each
experiment controls one dimension and lets the other emerge. A consumer
enforces both, which is conservative; it is not a jointly validated 2-D
(C, R) surface.

### Planned: joint-capacity

Not built. Today `concurrency-sweep` yields C_safe and `rate-capacity`
yields R_safe, each with the other dimension left free. `joint-capacity`
would validate (concurrency, offered_rps) combinations together --
open-loop arrivals at rate R with at most C in flight -- to get a real
2-D safe operating region, starting with points around the
recommendation, (C, 0.8R), (C, R), (0.8C, R), plus slightly-over
controls (1.2C, R), (C, 1.2R), each confirmed like any other point. It
needs a new runner (rate-driven arrivals with a concurrency cap).

## Running experiments

```bash
python3.11 -m venv .venv && .venv/bin/pip install -e ".[dev]"   # same install CI uses; installs bedrock-benchmark

bedrock-benchmark list                                                  # models + experiments, no AWS calls
bedrock-benchmark doctor --model nova-micro                             # ready? identity, access, quota, config
bedrock-benchmark plan concurrency-sweep --model nova-micro             # validate + estimate, no requests
bedrock-benchmark pilot concurrency-sweep --model nova-micro            # ~30 s smoke test
bedrock-benchmark run concurrency-sweep --model nova-micro              # the benchmark
bedrock-benchmark run all --all-models                                  # every experiment x every enabled model (explicit)
bedrock-benchmark run rate-capacity --model nova-micro --model nova-pro
bedrock-benchmark run concurrency-sweep --model nova-micro --slo-profile gold   # only gold workloads
```

Experiments are named, never pathed: `bedrock-benchmark` (`src/bedrock_benchmark/cli.py`)
maps a name to its file and finds the checkout itself, so the directory
layout is not part of the interface. It is a thin layer over the same
engine `scripts/run.py` / `scripts/run_all.py` call (still there for
backward compatibility).

`--slo-profile NAME` (repeatable) runs only the workloads bound to that
profile in `catalog/workloads.yaml`: an isolated sweep keeps its
matching workloads (`concurrency-sweep --slo-profile gold` runs just
`short_chat`); a mix runs only if every class matches -- a partial mix
is a different mix, so it's skipped; an experiment with nothing
matching is skipped. The plan lists every skip and why.

**Pilot run.** Before a long batch, `--pilot` sends a few sequential
requests (`--pilot-requests`, default 3) per model x workload the plan
would use -- after `--model` / `--slo-profile` / experiment filters --
and checks (`pilot.py`):

| Check | FAIL / WARN when |
|---|---|
| access | any non-throttle error (credentials, model access, region, request shape) -> FAIL |
| shape | input or output p50 outside the workload-validation tolerances (e.g. output stopping early) -> FAIL |
| slo | an UNLOADED TTFT / TPOT / E2E p50 already over the workload's p95 limit -> WARN (no load level can PASS) |
| quota | a throttle at one request at a time -> WARN (something else is using the quota) |

Results go to `results/pilot-<timestamp>/pilot.yaml` and are never
mixed into experiment data; the batch is never started by `--pilot`
(exit code 1 if anything FAILed). On nova-micro it takes ~30 s:

```
OK    nova-micro  short_chat                 gold    in 505/512   out 64/64     ttft 455ms  tpot 3.4ms  e2e 672ms
OK    nova-micro  rag_answer                 silver  in 4093/4096 out 256/256   ttft 404ms  tpot 3.2ms  e2e 1152ms
OK    nova-micro  long_generation            bronze  in 4095/4096 out 1024/1024 ttft 412ms  tpot 3.1ms  e2e 3614ms
OK    nova-micro  long_context_short_answer  silver  in 8103/8192 out 64/64     ttft 532ms  tpot 3.2ms  e2e 732ms
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
Exit code is non-zero if any run failed.
