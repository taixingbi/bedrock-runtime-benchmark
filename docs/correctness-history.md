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
