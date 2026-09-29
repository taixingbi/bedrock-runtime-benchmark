# capacity-profile.yaml schema

> Docs: [methodology](methodology.md) · [SLO statistics](slo-statistics.md) · [quota model](quota-model.md) · [capacity-profile schema](capacity-profile-schema.md) · [experiment design](experiment-design.md) · [correctness history](correctness-history.md) · [README](../README.md)

The benchmark's one deliverable and its contract with consumers. Schema version 23.

## The profile

Each run writes raw per-request JSONL and a `capacity-profile.yaml`
artifact under `results/` (gitignored -- these are real measurement outputs, not
checked-in fixtures). The `capacity-profile.yaml` schema (v23 -- see
[correctness history](correctness-history.md) for why `rate` and `concurrency` are always
kept in separate blocks, why the rate block separates offered load
from goodput, and why there's no `global_max_concurrency`). A
capacity-reference-rate result:

```yaml
schema_version: 23
experiment: capacity-reference-rate
mode: sweep  # sweep | sustain | history; focused retests retain the base experiment name
run: {run_id: 3f2a..., owner: alice, purpose: model onboarding, ticket: CAP-123, environment: dev}   # who ran it and why (bedrock-benchmark run --owner/--purpose/--ticket/--environment)
purpose: reference                                 # admission_calibration | characterization -- then recommendation is always null
environment:                                       # provenance -- see methodology.md, "Provenance and temporal validation"
  measured_at: {start: 2026-09-26T19:06:40+00:00, end: ...}
  account: "646821141010"
  region: us-east-1
  inference_profile: us.amazon.nova-micro-v1:0
  benchmark_version: 0.1.0
  git_commit: ...
  git_dirty: false
  runtime: {python: 3.11.16, boto3: ..., botocore: ...}
validity: {envelope: single_run_operating_envelope, repeated_runs: 1, days_observed: 1, scope: "single run -- ..."}
model: {name: nova-micro, provider: bedrock, model_id: ..., region: ...}
constraints:                                       # what every number was judged against
  quota: {account: "646821141010", region: us-east-1, rpm: 400, tpm: 8000000, output_burndown: 1.0}  # constraints/quota.yaml
  slo:                                                                       # constraints/slo.yaml -- POLICY input
    role: policy_input
    profiles:
      gold: {ttft_p95_ms: 800, tpot_p95_ms: 40, latency_p95_ms: null, success_rate_min: 0.995, throttle_rate_max: 0.001, confidence: 0.95}
  workloads:                                                                 # catalog/workloads.yaml -- the E2E cap is per workload
    short_chat: {input_tokens: 512, output_tokens: 64, slo_profile: gold, latency_p95_ms: 3000, role: reference}
measurement:
  warmup_s: 10
  window_s: 90
  throttle_pause_s: 0.0                     # concurrency sweeps: a worker's wait after a 429
  repetitions: 1
  window_policy: scheduled_in_window_for_rates__completed_in_window_for_throughput
  clock: monotonic_durations__wall_clock_timestamps
  gate: pass_fail_inconclusive              # every check is PASS / FAIL / INCONCLUSIVE
  confidence: 0.95
  confirmation: {max_looks: 2, max_repetitions: null, max_requests: 8000, max_duration_s: 1800, candidates: 1, cooldown_s: 120, warmup_s: 60}  # null = no repetition cap (shipped default)
  isolation: {inter_subject_cooldown_s: 120, refinement_cooldown_s: null, per_candidate_cooldown_s: 120}  # provider-state isolation
  min_requests_to_resolve_throttle_slo: 2995
sweep: {type: rate, quota_fractions: [0.25, ...], relative_to: provider_ceiling}
workload_classes:
  short_chat:
    slo_profile: gold
    role: reference                                # catalog/workloads.yaml
    observed: {input_tokens_p50: 505, output_tokens_p50: 61}
    workload_validation: {input: {...}, output: {...}, valid: true}
    provider_constraints: {tokens_per_request: 576.0, ceiling_rps: 6.6667, binding_constraint: rpm, ...}
    sweep_values_rps: [1.6667, 3.3333, ...]
    rate:
      observed_nonfailing_offered_rps: 8.3333      # highest point before the first FAIL...
      observed_verdict: INCONCLUSIVE               # ...but not enough requests to PROVE the SLO
      observed_inconclusive_checks: [{name: throttle_rate, n: 600, required_n: 2995, ...}]
      observed_slo_goodput_rps: 8.1
      statistically_confirmed_offered_rps: 5.0     # THE CAPACITY: highest confirmed SLO-compliant point -- null if none
      confirmed_slo_goodput_rps: 4.9
      measured_burst_ceiling_rps: 10.0      # highest swept rate that didn't FAIL (may be burst)
      provider_ceiling_rps: 6.6667          # from the quota
      saturation:                           # a DISCOVERY observation -- never the capacity
        observed_edge: 13.3333              # first FAIL seen by the discovery sweep (incl. refinement)
        phase: discovery                    # or refinement
        status: discovery_resolved          # discovery_not_reached / discovery_unresolved (+ unstable_region)
      summary: "statistically_confirmed=5.0 (the capacity) | observed_nonfailing=8.3333 (INCONCLUSIVE -- ...; not shown unsafe) | saturation=13.3333 (first FAIL in discovery -- an observed edge, not confirmed)"
    diagnosis:                              # MEASUREMENT interpretation: what limits the envelope
      bottleneck: provider_throttling       # provider_throttling_and_latency | latency | errors | not_reached | unresolved
      nominal_binding_constraint: rpm      # nominal quota context, not a causal diagnosis
      saturation_at: 13.3333
      failed_checks: [success_rate, throttle_rate]
      throttle_rate_at_saturation: 0.0464
      non_throttle_error_rate_at_saturation: 0.0
      attempted_rps_at_saturation: 9.9
      served_rps_at_saturation: 6.6
      provider_ceiling_rps: 6.6667
      latency_healthy_at_observed_nonfailing: true
      latency_at_observed_nonfailing: {ttft_p95: {observed: 488, threshold: 800, verdict: PASS}, ...}
    recommendation:                         # POLICY, kept apart from the measurement above
      admission_envelope:                   # null (+ reason) when nothing is statistically confirmed
        max_inflight: null                  # set by concurrency sweeps
        sustained_rps: 4.0                  # min(CONFIRMED x 0.8, ceiling x 0.9)
        scope: isolated_workload_class      # this class alone -- NOT additive across classes
        source: statistically_confirmed_measurement
        headroom_fraction: 0.2              # policy target
        quota_headroom_fraction: 0.1
        effective_headroom_fraction: 0.2    # 1 - sustained_rps / confirmed, after the quota cap
        evidence: single_run_operating_envelope   # never production-ready alone
        production_use: "not production-ready alone: repeat across times / days, then use `bedrock-benchmark validate`'s ..."
        binding: measurement                # or provider_quota
        basis: {statistically_confirmed_offered_rps: 5.0, provider_ceiling_rps: 6.6667}
    sweep_points: [{value: 1.6667, verdict: INCONCLUSIVE, phase: discovery, n: 150, inconclusive: [...]}, ...]  # phase: discovery | refinement
    load_generator:                         # open-loop validity: did arrivals start when scheduled?
      scheduling_lag_p50_ms: 0.4
      scheduling_lag_p95_ms: 1.1
      scheduling_lag_p99_ms: 2.3
      max_lag_ms: 9.8
      worst_point: {phase: discovery, value: 16.6667, lag_p99_ms: 3.1}
      limit_p99_ms: 50.0
      valid: true                           # false: the client lagged -- the run measured the client too
    evidence: {n: 1740, n_throttled: 0, throttle_rate_upper: 0.0017, verdict: {...}, peak_outstanding: 9, ...}
recommendation_policy: {headroom_fraction: 0.20, quota_headroom_fraction: 0.10}   # constraints/recommendation-policy.yaml
transport: {max_connections: 64, executor_workers: 64, total_max_attempts: 1, connect_timeout_s: 5, read_timeout_s: 60}
```

A concurrency sweep writes `concurrency: {observed_nonfailing,
observed_verdict, observed_nonfailing_rates, statistically_confirmed,
saturation, observed_slo_goodput_rps, provider_ceiling_rps,
observed_nonfailing_above_provider_ceiling, scope, summary}` instead of
`rate` (its `sweep_points` carry attempted / successful / throttled /
goodput rps and `ceiling_ratio`, and
`above_provider_ceiling: true` for points served >10% over the ceiling
(diagnostic -- confirmation still decides; the block adds
`confirmed_ceiling_ratio` / `confirmed_above_provider_ceiling`); confirmation
candidates carry `ceiling_ratio` and, when throttled while served far
below the ceiling, `throttled_below_ceiling: true`), and its
recommendation sets `max_inflight` instead of `sustained_rps` --
likewise from the confirmed point only. A sweep with
`stop_after_fails` records the values it never ran:
`sweep_stopped_early: {after_consecutive_fails: 2, skipped_values: [...]}`.

**Scope.** Every measurement block and admission envelope states
`scope`: `isolated_workload_class` (the class swept ALONE) or
`workload_mix` (a `mixed_workloads` entry). Per-class `max_inflight` (and `sustained_rps`) values are **isolated** limits -- each holds for that class running alone (`scope: isolated_workload_class`). They are not additive across classes and are not a global limit; only `capacity-mix-rate` (`scope: workload_mix`) measures classes together.

This is the actual deliverable -- not an HTML report. A gateway's own
config review reads this file, and decides its own global/tenant/AIMD
config FROM these per-class envelopes -- this repo never pre-packages
a gateway control policy itself (see [Consumers](#consumers)).


### Measurement vs recommendation (admission envelope)

A measured result is not an operational policy, so every workload class
or mix keeps them in separate blocks:

| Block | Holds | Kind |
|---|---|---|
| `rate` / `concurrency` | `observed_nonfailing_*`, `statistically_confirmed_*` (the capacity), `measured_burst_ceiling_rps`, `provider_ceiling_rps`, saturation, SLO goodput, verdicts -- never a safety margin | measurement |
| `recommendation.admission_envelope` | the confirmed point after the safety headroom in `constraints/recommendation-policy.yaml` | policy |

```
measured        statistically_confirmed_offered_rps = 6.67
policy          headroom_fraction = 0.20
recommendation  sustained_rps = 5.33
```

The recommendation says only: *based on this measured model/workload
envelope, this is the recommended maximum backend in-flight concurrency
and/or sustained offered rate after safety headroom*
(`recommendation.py`):

```
concurrency sweep   max_inflight  = floor(statistically_confirmed_concurrency x (1 - headroom))
rate sweep          sustained_rps = min(statistically_confirmed_offered_rps x (1 - headroom),
                                        provider_ceiling_rps x (1 - quota_headroom))
```

It fails closed: no statistically confirmed point -> `admission_envelope:
null` with a `reason` naming the cause (a confirmation candidate's
`stop_reason`, or the discovery point that stopped the fixed-sequence
test and its `n < required_n`); an observed or INCONCLUSIVE point is
never used.
A `max_inflight` that floors to 0 (e.g. confirmed C=1 with 20% headroom)
is also null -- 0 would admit nothing, and rounding up would drop the
headroom. Because rounding (and the quota cap) change the margin, every
envelope states `headroom_fraction` (the policy target) AND
`effective_headroom_fraction` (1 - recommended / confirmed), plus
`rounding_policy: floor` for concurrency: confirmed C=2 -> max_inflight
1 is a 50% effective margin, C=4 -> 3 is 25%. The quota term matters because a rate sweep deliberately goes
above quota and a short window can pass there on burst allowance --
observed serving, not a sustainable rate; `binding` says which term won.
Headroom defaults (20% off the measurement, 10% off the quota) are
recorded in `recommendation_policy`.

It deliberately emits nothing gateway-specific -- no global or tenant
concurrency, tenant RPM limits, queue waits, AIMD parameters or tenant
allocation. Mapping the envelope onto those is `eval-bedrock-gateway`'s
decision, which can apply its own margins on top (e.g. more for a
critical tenant).


## Machine-readable contract

The prose above explains the fields; the contract consumers check is a
JSON Schema shipped with the package, one per artifact type and version:

| Artifact | Version field | Schema |
|---|---|---|
| `capacity-profile.yaml` (one run) | `schema_version: 23` | [`capacity-profile-v23.json`](../src/bedrock_benchmark/schemas/capacity-profile-v23.json) |
| `temporal-capacity-profile.yaml` (repeated runs) | `artifact: temporal_capacity_profile`, `temporal_profile_schema_version: 1` | [`temporal-capacity-profile-v1.json`](../src/bedrock_benchmark/schemas/temporal-capacity-profile-v1.json) |

```bash
bedrock-benchmark validate-profile results/run-all-<ts>/nova-micro/*-capacity-profile.yaml
```

The schemas pin what a consumer decides on -- versions, `purpose`,
`validity.envelope`, `admission_envelope` (`source`, `evidence`,
`scope`, headroom), `measurement_validity.status`, saturation status,
`transport.total_max_attempts: 1`, and for temporal entries the
`status` and `production_capacity_input` -- and allow additional
properties, so additive fields don't break consumers. `run:` is
optional because v23 profiles written before it exist. Every profile the
test suite builds is validated against the schema, and `publish` refuses
a run whose profiles don't conform.

## Publishing

`bedrock-benchmark publish <run dir> --destination s3://bucket/prefix`
(or a directory) copies the run unchanged to
`<destination>/<YYYY>/<MM>/<run dir>/` with a `manifest.yaml`:
`owner`, `run_id`, `purpose`, `ticket`, `environment`, `measured_at`,
`evidence: single_run_operating_envelope`, one line per profile (model,
experiment, purpose, schema) and every file's size + sha256. The
manifest is written last (its presence means the upload completed) and
never overwritten: a published run is immutable.

## Consumers

The benchmark knows nothing about any gateway's configuration schema.
A consumer maps `recommendation.admission_envelope` (and, if it wants,
the raw measurement) onto its own knobs and margins. For
`eval-bedrock-gateway` that is `app/scripts/capacity_review.py`
(`services/gateway/capacity_review.py`), which compares profiles against
a snapshot of the gateway's limits -- it moved there from this repo so
that the producer doesn't depend on the consumer.

Contract rules a consumer can rely on:

- `recommendation.admission_envelope` is derived only from a
  statistically confirmed point, and only in a `purpose: reference`
  profile; it is `null` (with `reason`) otherwise. A characterization
  profile never carries one -- skip it.
- A `purpose: admission_calibration` profile carries, per workload, a
  `calibration_point`: `{workload_shape: {input_tokens, output_tokens},
  slo_profile, tokens_per_request, statistically_confirmed_concurrency,
  confirmed_rates: {attempted_rps, successful_rps, throttled_rps,
  slo_goodput_rps}, ceiling_ratio, observed_saturation_edge, bottleneck,
  scope, use}` -- statistically confirmed, no headroom. Rates are kept apart -- `attempted_rps` (inflated by fast 429s under overload), `successful_rps` (served), `throttled_rps`, `slo_goodput_rps` -- plus `ceiling_ratio` (served / nominal ceiling); all are observations of what concurrency and latency produced, not a tested rate like `capacity-reference-rate`'s `sustained_rps`. Different workload shapes may require different concurrency to reach the same provider rate ceiling; therefore concurrency must not be interpreted as a workload cost weight.
  It is workload-specific admission evidence (calibration_point -> workload-specific admission evidence -> policy derivation (gateway) -> mixed validation (eval-bedrock-platform)),
  not a limit to compare a config against (the gateway's review skips
  these profiles).
- `max_inflight` is set for concurrency sweeps, `sustained_rps` for rate
  sweeps; the other is `null`. They are two independent guardrails, not
  a jointly validated (C, R) region.
- Every admission envelope is `evidence: single_run_operating_envelope`.
  A production capacity value comes from repeated runs through
  `bedrock-benchmark validate` (the temporal-capacity-profile's
  `production_capacity_input`, status `VALID` / `VALID_CONSERVATIVE`),
  never from one profile.
- **Production consumers take a capacity input only when it is
  statistically confirmed AND `measurement_validity.status` is `valid`
  (or knowingly `suspect_reproduced`) AND temporally validated**
  (`production_capacity_input` non-null -- temporal validation already excludes
  `invalid` runs). Anything else is evidence at most, never config.
- Every class/mix carries `measurement_validity: {status, events,
  recovery_probes}` -- see [SLO statistics](slo-statistics.md),
  "Measurement validity". `invalid` means the provider never passed a
  recovery probe: nothing from that subject is a capacity conclusion.
- A calibration point never becomes gateway config directly: calibration
  point -> derived workload policy -> mixed-traffic validation ->
  temporal validation -> gateway config (not `C_confirmed = 6` ->
  `max_inflight = 6`).
- `scope: isolated_workload_class` values hold for that class alone:
  never sum them across classes or treat one as a global limit (the
  gateway's review takes the minimum across classes).
- Nothing gateway-specific (tenant limits, queues, AIMD, allocation) is
  ever emitted.
- `schema_version` increases on any change to these fields; see
  [correctness history](correctness-history.md).

## Common descriptive metrics (metrics_version 1)

All experiments expose the same `metrics` structure:
- `latency`: TTFT, TPOT and E2E, each with p50/p95/p99 and valid sample count.
  These describe successful requests scheduled within the window, including
  those finishing during drain. Missing distributions are null with n=0.
- `throughput`: scheduled and actual SDK-start RPS, successful completion RPS,
  SLO goodput, and input/output/total tokens per second. Completions inside the
  window count, including requests scheduled earlier. Token counts come from
  provider usage; missing usage makes the affected total null, with coverage
  counts reported. Zero completions means zero throughput.
- `reliability`: scheduled-cohort success, throttle, non-throttle error and timeout
  rates, plus counts of stream failures before/after first text. Timeout overlaps
  the error category. Legacy rows without stream metadata are explicitly counted
  as unknown. No scheduled requests means null rates.
- `load_state`: configured concurrency or offered RPS, scheduling-lag
  p50/p95/p99, and peak/time-weighted average outstanding. SDK calls use
  started_at→completed_at; queue-inclusive outstanding uses
  submitted_at→completed_at. Both are clipped to the measurement window.
  Queue-inclusive values are null for legacy data without submission timestamps.
  Open-loop rate runs have no explicit max_inflight cap (null); executor_workers
  is a worker-pool size, not a cap on queued requests.

Capacity experiments store these under each subject's `measurement_windows`,
with phase, candidate value, repetition, aggregate metrics and continuous 30s bins.
Empty windows and final partial bins are retained. Mixed windows additionally
contain `classes`, each with the same structure, including classes with no samples.
Class load settings are null because only the overall mix has a configured load.
Recovery attempts remain separate windows; diagnostic windows cannot confer PASS.

History experiments expose the same structure at
`history_comparison[].aggregate.metrics` and `history_comparison[].bins[].metrics`,
using their configured bin duration. Existing summary fields remain available.

TPOT retains its existing SLO definition:
(stream completion − first non-empty text delta) / (output_tokens − 1).
The additive `text_decode_tpot_ms` distribution instead ends at the last
non-empty text delta, excluding trailing metadata time. Neither metric measures
individual token gaps: a stream delta may contain multiple tokens.
The new raw `last_text_at` and `last_text_latency_ms` fields preserve that boundary.
On stream errors, first/last text timestamps are retained and
`stream_failure_stage` identifies before/after first text; partial usage is never
invented. Latency durations use the existing monotonic clock.

Bins and p99 are descriptive and introduce no new SLO gates. Small sample counts
do not establish stable tail latency. Historical artifacts are unchanged; the
existing schema version accepts these additive fields.
