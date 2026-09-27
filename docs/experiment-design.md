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
scripts/                   entry points only (run.py, run_all.py, fetch_quota.py, drift.py)
        ↓
every experiment x every model -> capacity-profile.yaml (judged against the constraints)
```

- **Models** -- `catalog/models.yaml`: the five models
  `bedrock-runtime-gateway` certifies (nova-micro, nova-lite, nova-pro,
  llama3-3-70b, qwen3-32b). `enabled: false` skips one by default.
- **Experiments** -- `experiments/*.yaml`: model-agnostic workload +
  sweep definitions, each with an explicit `purpose` (see
  [Reference vs characterization](#reference-vs-characterization)).
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
| `concurrency-sweep.yaml` | reference | each of `short_chat` / `rag_answer` / `long_generation` alone, concurrency 1..48 until 2 consecutive FAILs; confirms the top 2 non-failing concurrencies (e.g. C=2 then C=4) | ~1.5 h |
| `mixed-capacity.yaml` | reference | 0.25x-2.5x ceiling, 60% `short_chat` / 30% `rag_answer` / 10% `long_generation` | ~32 min |
| `token-sweep.yaml` | characterization | the 4 characterization shapes x concurrency 1..24 until 2 consecutive FAILs | ~1 h |

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

# experiments/token-sweep.yaml -- the four characterization shapes
workloads: [tiny_request, medium_context, long_context_short_answer, very_large_context]
```

The catalog is deliberately broader than any experiment: adding a
workload sends no traffic, only experiments that list it do.
`long_context_short_answer` isolates input-side (prefill) pressure and
`long_generation` output-side (decode) pressure; the former is also
TPM-bound on low-TPM models (llama3-3-70b: ~1.21 rps by TPM vs 1.33 by
RPM).

## Reference vs characterization

Not every catalog workload gets a capacity recommendation.

| | Workloads (`role`) | Experiments (`purpose`) | Profile carries |
|---|---|---|---|
| **reference** | `short_chat` (gold), `rag_answer` (silver), `long_generation` (bronze) -- one per tier | `rate-capacity`, `concurrency-sweep`, `mixed-capacity` | measurement **and** `recommendation.admission_envelope` |
| **characterization** | `tiny_request`, `medium_context`, `long_context_short_answer`, `very_large_context` | `token-sweep` | measurement only -- `admission_envelope: null` |

A production envelope comes only from a reference experiment, and a
reference experiment may only list reference workloads (the loader
rejects anything else). A characterization experiment may use any
workload, reference ones included, to study how token shape and
context size move the envelope; it measures and confirms, but its
`recommendation` is always null. Both are declared: `role` in the
catalog, `purpose` in each experiment, and the profile records both.

Responsibilities, without overlap:

| Experiment | Produces |
|---|---|
| `concurrency-sweep` | per-class isolated `max_inflight` for the three reference workloads |
| `rate-capacity` | per-class isolated `sustained_rps` for the same three |
| `mixed-capacity` | `sustained_rps` for one given mix of them |
| `token-sweep` | token / context-shape characterization only (no reference workloads) |

Per-class `max_inflight` (and `sustained_rps`) values are **isolated** limits -- each holds for that class running alone (`scope: isolated_workload_class`). They are not additive across classes and are not a global limit; only `mixed-capacity` (`scope: workload_mix`) measures classes together.

**Concurrency ranges.** Concurrency is absolute, but the quota ceiling
is reached at C ~= ceiling_rps x latency (Little's law) -- on nova-micro
~4 for `short_chat`, ~14 for `rag_answer`, ~43 for `long_generation`. So
one wide list serves every class, and `sweep.stop_after_fails: 2` ends a
class's discovery after two consecutive FAILs (saturation seen twice,
enough for the non-monotonic check) instead of sweeping a throttle storm
far past it; the profile lists `sweep_stopped_early.skipped_values`. The
dry-run marks those estimates `<=`: they assume every value runs.
`sweep.refine_max_points: 3` then bisects the bracket around saturation
(e.g. 8 PASS / 12 FAIL -> 10 -> 11 or 9), so the confirmed candidate is
the real edge rather than the coarse grid point below it; refinement
points appear in `sweep_points` with `phase: refinement`.

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
.venv/bin/python scripts/run_all.py --model nova-micro --slo-profile gold   # only gold workloads
.venv/bin/python scripts/run_all.py --model nova-micro --pilot          # ~30 s smoke test, no batch
```

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
