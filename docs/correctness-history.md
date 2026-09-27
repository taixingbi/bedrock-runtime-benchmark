# Correctness history

> Docs: [methodology](methodology.md) · [SLO statistics](slo-statistics.md) · [quota model](quota-model.md) · [capacity-profile schema](capacity-profile-schema.md) · [experiment design](experiment-design.md) · [correctness history](correctness-history.md) · [README](../README.md)

Every measurement-correctness bug found in review, by the capacity-profile schema version that fixed it. Kept as design history: each entry explains a rule the current code enforces.

## Schema v2 fixes

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
   workloads" in [methodology](methodology.md)) is the only valid way to answer that question.
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

## Schema v3 fixes

7. **Drain completions inflated throughput.** `ConcurrencyRunner`'s
   last batch (fired just before `duration_s`) finished after it, yet
   `throughput = successes / duration_s` counted those completions in
   the numerator without extending the denominator -- overstating
   throughput and SLO goodput by up to one concurrency level's worth
   of requests per point. Fixed by the measurement policy ([methodology](methodology.md)).
8. **The rate block mixed goodput with offered load.**
   `measured_sustainable_rps` was the best point's SLO goodput, and
   `production_rps` applied headroom to that goodput -- but a gateway
   admission limit is on offered load. Split into
   `max_safe_offered_rps` / `slo_goodput_rps` /
   `production_offered_rps` (v7: `observed_nonfailing_offered_rps` /
   `statistically_confirmed_offered_rps` / `production_sustained_rps`,
   quota-capped and derived from the confirmed point only).
9. **A 0.1% throttle SLO was gated on too few samples.** See
   "Verdicts" in [SLO statistics](slo-statistics.md).

## Schema v4 fixes

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
12. **Rate sweeps ignored TPM.** See [quota model](quota-model.md).
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
    the artifact's `constraints:` block); loaders reject copies. SLO
    profiles are bound explicitly per workload in the workload catalog
    (no implicit default) and defined by request class, not model;
    quotas are scoped by account and region, matching how Bedrock
    actually applies them.

## Schema v6 fixes

18. **The 95% bounds were computed but never gated.** No SLO set
    `confidence`, so a few hundred clean requests "passed" a 0.1%
    throttle SLO they couldn't statistically demonstrate. Now every
    check is PASS / FAIL / INCONCLUSIVE -- see "Verdicts".
19. **Production rate could exceed quota.** 20% headroom off a rate
    that passed at 1.8x quota (burst) still recommended 1.44x quota.
    Production is now also capped by the provider ceiling -- see
    "Production rate".
20. **One snapshot per point.** Repetitions defaulted to 1 everywhere.
    The boundary is now re-measured in a confirmation phase -- see
    "Two-phase sweep".
21. **TTFT + TPOT alone missed user-visible E2E.** Each workload now
    carries its own E2E cap -- see "SLO profiles".

## Schema v7 fixes

22. **"Measured safe" could be INCONCLUSIVE.** v6's
    `measured_safe_offered_rps` held the best non-failing point even
    when it was INCONCLUSIVE, and production was derived from it --
    treating "no violation observed" as "SLO proven". Now:
    `observed_nonfailing_*` (may be INCONCLUSIVE) ->
    `statistically_confirmed_*` (PASS only, or null) -> production
    derived from the confirmed point only (null otherwise).

## Schema v8 fixes

23. **Confirmation reused discovery data.** v7 pooled the confirmation
    repetitions with the discovery sample that had selected the point.
    Confirmation now uses only its own independent data; discovery only
    selects candidates.
24. **Unplanned looks.** Adaptive repetitions with a PASS check after
    each one inflate false PASSes (7.0% vs 5% simulated). PASS is now
    allowed only at pre-planned sample sizes with Bonferroni-corrected
    confidence; caps end in INCONCLUSIVE.
25. **Wilson bounds under-covered at zero events** (~93% real coverage
    for a stated 95% at gold's limit). Rate checks now use the exact
    Clopper-Pearson bound: 2,995 requests for 0.1%, not 2,703.
26. **"Confirmed" skipped INCONCLUSIVE points.** Without a confirmation
    phase, the confirmed point is now the top of the leading run of
    strict PASSes (a fixed-sequence test), not the best-goodput PASS
    anywhere before the first FAIL.

## Schema v9 fixes

27. **Latency SLOs were judged on the sample percentile alone.** "Sample
    p95 <= 800 ms" says nothing about how sure we are the true p95 is.
    Each p95 limit is now an exceedance proportion,
    `P(value > T) <= 5%`, gated by the exact binomial bound like
    throttle -- PASS / FAIL / INCONCLUSIVE with `exceedances`,
    `exceedance_rate_upper` and `required_n` on every latency check. The
    confirmation plan covers latency checks too. SLO definitions are
    unchanged.

## Schema v10 fixes

28. **Latency verdicts now also report the bound in milliseconds**
    (`p95_upper_bound`, order-statistic UCB, exactly dual to the
    exceedance test).
29. **No provenance.** Profiles now carry `environment` (measured_at,
    account, region, inference profile, benchmark version, git commit)
    and `validity`; `scripts/drift.py` compares repeated runs.
30. **The SLO is marked as policy input** (`constraints.slo.role:
    policy_input`) -- externally supplied, never derived from results.

## Schema v11 fixes

31. **Measurement and policy were mixed.** `production_sustained_rps` /
    `production_max` (headroom applied) sat inside the measurement
    blocks. They're now `recommendation.admission_envelope`
    (`max_inflight` / `sustained_rps`, `source`, `headroom_fraction`,
    `binding`, `basis`) per class/mix, next to -- not inside -- the
    unchanged confirmed measurement; headroom settings moved to
    `recommendation_policy`. A max_inflight that floors to 0 is null
    rather than rounded up.
32. **TPM reservation vs consumption.** The ceiling used input +
    max_tokens x burndown for both; AWS reserves input + max_tokens at
    admission and settles at input + output x burndown. Both are now
    reported (`reservation_tokens`, `consumption_tokens`,
    `token_pressure`) and the larger bounds the TPM ceiling. No change
    for burndown-1 models.
33. **Discovery-only sweeps could never confirm anything.** Only
    `rate-capacity` had a confirmation phase; `concurrency-sweep`,
    `token-sweep` and `mixed-capacity` judged one window per point, and
    the fixed-sequence test stops at the first non-PASS point -- so a
    clean but too-small low point (gold C=4: 796 requests, 0 throttles,
    0.38% bound vs 0.1%) left every class unconfirmed, and the
    `recommendation.reason` still said "see `confirmation` ... raise the
    confirmation caps" when no such phase existed. Every shipped
    experiment now has a `confirmation:` block (the mix with caps sized
    for its gold class's 60% share), and `reason` states the actual
    cause: the confirmation candidate's `stop_reason`, or -- discovery
    only -- the point the test stopped at and its `n < required_n`.
    Field set unchanged.
34. **Mix looks were timed on expected class counts.** A mix's look was
    at total >= class requirement / share (6,147 for a 60% gold class),
    but random class draws can leave that total with only 3,600
    `short_chat` requests -- below the 3,688 its look needs. Looks are
    now taken only when each class's ACTUAL n (and the blend's total)
    meets its own requirement (`plan.look_requirements`).
35. **The canonical concurrency sweep paused after 429s.** 81f95ae set
    `throttle_pause_s: 1.0` in `concurrency-sweep`, which lowers offered
    load and flatters the throttle rate `max_inflight` comes from.
    Removed: the canonical benchmark measures pure closed-loop
    concurrency; backoff belongs in its own experiment. Profiles
    measured at 81f95ae (`measurement.throttle_pause_s: 1.0`) aren't
    comparable with later ones at the throttling points.
36. **Drift was an auxiliary report.** `scripts/drift.py` now emits a
    formal `temporal_validation` block (runs, days / UTC hours observed,
    confirmed min/median/max, conservative values, and an `envelope`
    verdict), and each profile's `validity.envelope` says
    `single_run_operating_envelope`.

## Schema v12 fixes

37. **Two capacity definitions.** `observed_nonfailing` was the
    highest-SLO-goodput point in the leading non-failing run (docs
    called that "the recommended concurrency"), while the recommendation
    came from the statistically confirmed point. Now one definition:
    capacity = the highest statistically confirmed SLO-compliant
    operating point. `observed_nonfailing` is the highest non-failing
    point, and SLO goodput is reported but selects nothing. A mix's
    `classes_at_recommended_point` (which showed the observed point) is
    now `classes_at_confirmed_point`.
38. **Every catalog workload looked recommendable.** Workloads now carry
    `role: reference | characterization` and experiments `purpose:
    reference | characterization`. Only a reference experiment -- which
    may list only the three reference workloads -- produces an
    admission envelope; a characterization profile (`token-sweep`)
    always has `admission_envelope: null`. Both are recorded in the
    profile.
39. **Policy sat in measurement files.** Headroom moved from every
    `experiments/*.yaml` to `constraints/recommendation-policy.yaml`
    (experiments with it are rejected), and `apply_headroom` left the
    measurement module -- policy is applied only in
    `recommendation.py`.
40. **The benchmark read gateway infrastructure.** `fetch_quota.py`
    tried `bedrock-runtime-gateway`'s DynamoDB quota table before AWS
    Service Quotas. Removed: the producer depends only on Bedrock / AWS.

## Schema v13 fixes

41. **concurrency-sweep's confirmation budget couldn't fit both
    candidates.** With C=6 failing, the fixed sequence confirms C=2
    (~388 requests/rep -> 10 reps to gold's first look, ~1,300 s) before
    C=4 (~5 reps, ~650 s): ~1,950 s against a 1,800 s cap, so C=4 always
    ended INCONCLUSIVE (`max_duration`), and C=2's 10 reps were at the
    10-rep cap. Now `max_duration_s: 3000`, `max_repetitions: 12`; the
    candidate order is unchanged.
42. **Rounding hid the real margin.** Confirmed C=2 at 20% headroom
    floors to max_inflight 1 -- a 50% margin. Every admission envelope
    now states `effective_headroom_fraction` next to the target
    `headroom_fraction` (and `rounding_policy: floor`).
43. **One number invited misreading.** Each rate/concurrency block has a
    `summary` stating confirmed / observed / saturation together, so an
    INCONCLUSIVE point in between reads as "not proven", not "unsafe".
44. **No bottleneck stated.** Each class/mix has a `diagnosis`: the
    bottleneck (`rpm_quota` / `tpm_quota` / `latency` /
    `quota_and_latency` / `errors`) read from the saturation point's
    failed checks, with throttle vs other error rates, attempted vs
    served rate against the provider ceiling, and latency health at the
    observed point.
45. **Only short_chat had a concurrency envelope.** `concurrency-sweep`
    now sweeps all three reference workloads (1..48, stopping after two
    consecutive FAILs -- `sweep.stop_after_fails`), and `token-sweep`
    covers only the four characterization shapes instead of repeating
    reference workloads. Every block and envelope states `scope:
    isolated_workload_class | workload_mix`: per-class values are not
    additive and are not a global limit.
46. **git_commit was read when the report was written**, so a commit
    made during a long run was attributed to it (a run started at
    67fb23b recorded b10099f). It's now captured once at process start.
47. **The capacity module still described goodput selection.**
    `analysis/capacity.py`'s docstring said "pick max SLO-goodput ->
    apply headroom"; it now states capacity = highest statistically
    confirmed SLO-compliant point, measurement only.
48. **"Confirmation is the only source of capacity" was a convention.**
    A reference experiment without `confirmation:` would have produced
    a discovery-only confirmed point. The loader now rejects it.
49. **Capacity was quantized to the coarse grid.** Concurrency sweeps
    bisect the non-FAIL / FAIL bracket around saturation before
    confirmation (`sweep.refinement: {strategy: integer_bisection,
    stop_when_adjacent: true, max_points: 4}`, `phase: refinement`); 4
    points resolve the widest shipped gap, 32 -> 48. Also: the
    1,800 s-budget result "confirmed C=2" was budget-limited
    (`unreachable_within_caps` for C=4), not evidence that C=4 is unsafe.
50. **Arrival-scheduler lag was unchecked.** Each class/mix reports
    `load_generator` scheduling lag with a `valid` flag (worst point's
    p99 <= 50 ms). Additive to v13 -- no consumer field changed.
## Schema v14 fixes

51. **A mix was gated on its blend too.** The blend was held to the
    strictest class's success/throttle limit, which can FAIL a mix whose
    every class meets its own SLO (gold 99.5% + silver/bronze 99.0% ->
    ~99.3% blend). A mixed point is now PASS exactly when every class
    PASSes its own profile; the blend is reported, never gated, and the
    confirmation plan has no blend group.
52. **Looks were evaluated on "all n >= N_j".** A fixed-duration
    closed-loop repetition's request count depends on outcomes, so the
    tested n was outcome-dependent. Looks now use exactly the first N_j
    requests (first N_c,j per class in a mix); candidates report the
    `decision` sample. A simulation with outcome-dependent counts is in
    `tests/test_confirmation.py`.
53. **Latency percentiles and latency gates read different
    populations.** Reported p50/p95/p99 used every request with a
    latency (429s included); the exceedance gate used successes. Both
    now use successful requests only.
54. **Rounded rates decided verdicts.** Observed-violation checks now use
    exact counts (`n_success`, `n_throttled`); the rounded rates are for
    reporting.
55. **Repetitions looked like the statistical unit.** With fixed-count
    looks (51-54) the unit is the request; `confirmation.max_repetitions`
    is now optional (default none -- caps are `max_requests` and
    `max_duration_s`) and no shipped experiment sets it: a candidate
    stops on a PASS look, an observed violation, `max_requests` or
    `max_duration_s` -- never on a count of repetitions.

## Schema v15 fixes

56. **token-sweep's output wasn't usable for gateway configuration.** As
    `characterization` it measured shape effects but was explicitly
    never a recommendation. It is now `purpose: admission_calibration`:
    each shape, under its own business SLO, gets a statistically
    confirmed `calibration_point` (shape, SLO, confirmed concurrency,
    `achieved_rps` -- observed, not a tested rate -- goodput,
    saturation, bottleneck, `scope: isolated_workload_class`) -- an input for deriving
    gateway admission classes / weights, still no envelope and no
    headroom. It requires `confirmation:` like reference. The sweep now
    spans 1..48 (the long shapes can saturate above 24).

## Schema v16 fixes

57. **calibration_point's rate read like a tested rate.** In a
    closed-loop sweep the rate is produced by concurrency and latency,
    so `confirmed_request_rate_rps` is now `achieved_rps` (an
    observation), distinct from `rate-capacity`'s `sustained_rps`. Points
    add `scope: isolated_workload_class`, C_safe is stated as a function
    of the measured provider environment too, and derived admission
    classes / weights must be validated under mixed traffic
    (`mixed-capacity`) before production use.
58. **mixed-capacity was described as validating gateway policy.** It
    calls Bedrock directly -- no gateway in the path -- so it calibrates
    R_safe for one explicit mix (backend evidence for a mixed-rate
    guardrail); validating the deployed gateway policy belongs to
    `bedrock-platform-eval`. Docs, `token-sweep.yaml` and
    `calibration_point.use` corrected; the YAML and docs state that one
    mix gives R_safe(that mix), not a global R_safe. No field changed.
59. **"global" mixed-rate wording.** mixed-capacity's output is a
    mix-scoped total-rate limit: statistically confirmed R_safe(mix) ->
    recommendation headroom -> R_admission(mix). Docs now say so; R_safe
    alone is never gateway config, and other mixes are separate
    experiment files. No field changed.
