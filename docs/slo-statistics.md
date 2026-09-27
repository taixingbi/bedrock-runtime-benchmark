# SLO statistics

> Docs: [methodology](methodology.md) · [SLO statistics](slo-statistics.md) · [quota model](quota-model.md) · [capacity-profile schema](capacity-profile-schema.md) · [experiment design](experiment-design.md) · [correctness history](correctness-history.md) · [README](../README.md)

How a measured point becomes PASS / FAIL / INCONCLUSIVE, why latency, success and throttle all use exact binomial bounds, and how adaptive confirmation keeps the false-PASS rate at or below 5%. The SLO itself is an externally supplied policy input (`constraints/slo.yaml`), never derived from measurements.

## SLO profiles

SLOs live in `constraints/slo.yaml`, separate from workloads, as three
service classes by business criticality of the request -- not by
model. The same model serves every class and gets one envelope per
class (strict gold -> lower safe rps/concurrency, relaxed bronze ->
higher), which maps onto a gateway's `request_class -> concurrency /
rate limit`.

Classes gate on the two latency components -- **TTFT** (time to first
token) and **TPOT** (time per output token after the first:
`(latency - TTFT) / (output_tokens - 1)`, per streamed request with
>= 2 output tokens) -- rather than end-to-end latency, so a class stays
meaningful for a 64-token reply and a 1024-token generation alike. A
configured TPOT SLO with no TPOT measured fails closed, like TTFT.

| Profile | For | Workload | TTFT p95 | TPOT p95 | Success | Throttle |
|---|---|---|---|---|---|---|
| `gold` | real-time, latency-sensitive, business-critical | `short_chat` | 800 ms | 40 ms | 99.5% | 0.1% |
| `silver` | standard synchronous application | `rag_answer` | 1.5 s | 70 ms | 99% | 0.5% |
| `bronze` | async, batch, throughput-oriented | `long_generation` | 3 s | 120 ms | 99% | 1% |

**End-to-end latency is workload-level, not profile-level.** TTFT and
TPOT generalize across output lengths; E2E doesn't -- a 64-, 256- and
1024-token output can't share one budget, and good TTFT + TPOT can still
add up to an unacceptable total. So each workload sets its own
`latency_p95_ms` cap in `catalog/workloads.yaml` (starting values:
short_chat 3s, rag_answer 10s, long_generation 60s -- set them to what
each product promises), applied on top of its profile.

Isolated workloads are gated on their own
profile. In a mix, every request counts toward goodput against its own
class's profile, every class is gated on its own profile, and the blend
on the STRICTEST success/throttle gate among its classes' profiles
(latency always per class). Resolving a throttle limit statistically
needs 2,995 requests per point for gold's 0.1%, 598 for silver's
0.5%, 299 for bronze's 1% (exact bound, 95% confidence, zero events).

### Verdicts: PASS / FAIL / INCONCLUSIVE

Insufficient evidence is not failure. Every check at every point gets a
verdict (`capacity.py`'s `evaluate`):

- **latency checks** (TTFT / TPOT / E2E p95): a p95 limit T is the
  statement "at most 5% of requests exceed T", so it's judged as an
  exceedance PROPORTION with the same exact bound as throttle -- k of n
  successful requests over T (a request with no measurement counts as
  over; nothing measured at all FAILs):
  - k / n > 5% (the sample p95 is over T) -> **FAIL**
  - exact 95% upper bound on k / n <= 5% -> **PASS**
  - otherwise -> **INCONCLUSIVE** with `required_n`. A sample p95 under
    T isn't enough: 30 clean requests still bound the exceedance at
    9.5%; 59 clean ones resolve it (93 with one slow request). 11 slow of
    500 is 2.2% observed, 3.6% bound -> PASS.

  This is the distribution-free test of H0: q95 > T vs H1: q95 <= T.
  Each latency check also reports it in milliseconds as
  `p95_upper_bound` -- the order-statistic one-sided 95% upper
  confidence bound on the true p95 (e.g. "p95 estimate 742 ms, UCB
  796 ms <= 800 ms -> PASS"). The two are exactly dual: PASS <=> UCB <= T
  (checked on random samples in `tests/test_latency_bounds.py`), so
  `statistically_confirmed` really means latency, success AND throttle
  are all statistically confirmed.
- **rate checks** (success, throttle), on EXACT one-sided
  (Clopper-Pearson) bounds at `confidence` (default 95%):
  - observed violation (e.g. throttle rate above the limit) -> **FAIL**
  - the bound clears the limit -> **PASS**
  - no violation, but too few requests to prove it -> **INCONCLUSIVE**,
    with `n` and `required_n` (0 throttles in 540 requests has a 95%
    upper bound of ~0.55% -- resolving a 0.1% limit needs 2,995)

Exact, not Wilson: these checks sit at 0-2 events, exactly where Wilson
is anti-conservative. Its 95% bound clears 0.1% after 2,703 clean
requests, but a service throttling at exactly 0.1% produces 0 throttles
in 2,703 requests 6.7% of the time -- a stated 95% that is really ~93%.
Clopper-Pearson (2,995 requests; 0.999^2995 = 0.050) has guaranteed
coverage.

A point is FAIL if any check fails, else INCONCLUSIVE if any is
inconclusive, else PASS. Saturation is the first FAIL.

**Not observing a violation is not the same as proving the SLO**, so
the artifact keeps three numbers apart and never lets one stand in for
another:

```
observed_nonfailing          best point before the first FAIL -- may be INCONCLUSIVE
      |
statistically_confirmed      THE CAPACITY: highest strictly-PASS point before the first FAIL -- or null
      |
recommendation.              derived ONLY from the confirmed point, after headroom
  admission_envelope         (and quota-capped); null when nothing is confirmed
```

`sweep_points` lists every point's verdict. Consumers only ever get a
recommendation from a confirmed point: an INCONCLUSIVE observed point
contributes nothing, and a class with no confirmed point has
`admission_envelope: null` (see [schema](capacity-profile-schema.md)).

Sample size decides what can be confirmed: at 95%, resolving a limit
with zero bad events takes 2,995 requests for gold's 0.1% throttle,
598 for silver's 0.5%, 299 for bronze's 1% -- and more once any event
occurs (4,742 for gold with one throttle). The confirmation phase below
exists to collect exactly that, and only that, at the boundary.

### Discovery -> refinement -> adaptive confirmation

One 90s window per point is a capacity snapshot. The sweep runs in
phases whose data is never mixed (`analysis/confirmation.py`):

```
coarse discovery  ->  bracket saturation  ->  local refinement  ->  independent confirmation
1 2 4 6 8 12(FAIL)     L=8, F=12              10, then 11 or 9       fresh reps at the candidates
```

| Phase | Data | Used for | Never used for |
|---|---|---|---|
| **discovery** | every sweep value, `repetitions` each | observed verdicts, saturation, transition region, **choosing candidates** | confirming anything |
| **refinement** (concurrency, `sweep.refine_max_points`) | bisection between the last non-failing point L and the first FAIL F | moving the bracket to the real edge, so candidates aren't stuck at the coarse grid point below it | confirming anything |
| **confirmation** | fresh repetitions at the candidates only | **the only source of `statistically_confirmed`** (and so of production values) | -- |

Refinement matters for the recommendation: with 8 PASS / 12 FAIL and a
real edge at 11, the coarse grid can only confirm 8 -> max_inflight
floor(6.4) = 6; after refinement it can confirm 11 -> 8. It is still
discovery-class data -- it only moves the candidates.

**A reference experiment must have `confirmation:`** -- the loader
rejects one without it, so "discovery picks, confirmation decides" is an
invariant of every profile that carries a recommendation, not a
convention.

Reusing discovery data to confirm the point it selected would be
double-dipping: the point was picked *because* its discovery sample
looked good. So confirmation starts from zero.

**Candidates.** The highest point(s) of discovery's leading non-failing
run -- for a rate sweep, only at or below the provider ceiling
(production is quota-capped anyway; above it a point passes on burst
allowance at best); for a concurrency sweep, the highest non-failing
concurrency. With `candidates: N > 1` they're tested
lowest-first and stop at the first one not confirmed (a fixed-sequence
test, which keeps the family-wise error at alpha without splitting it).
`concurrency-sweep` uses 2: with C=6 failing, C=2 is confirmed first and
C=4 only if C=2 passes -- a FAIL at C=4 still leaves C=2 confirmed. Keep
that order even though the lower candidate is the slow one (C=2 needs
~10 repetitions to reach gold's 3,688, C=4 ~5): the budget must fit
both, so `concurrency-sweep` has `max_duration_s: 3000` and
`max_repetitions: 12` -- at 1,800 s C=4 always ran out of time.
`cooldown_s` idles between the phases so discovery's overload (the
saturation point is the last one swept) doesn't bleed into the first
confirmation repetition; it isn't counted against `max_duration_s`.

**Adaptive, but no peeking.** Repetitions are added one at a time, but
a PASS can only be declared at `max_looks` sample sizes fixed before any
confirmation data exists -- look j is where j-1 bad events would still
clear the limit -- each at confidence `1 - 0.05 / max_looks`
(Bonferroni). FAIL (an observed violation) stops it at any time.
Checking the bound after every repetition and stopping on the first
clear would inflate false PASSes; simulated at a true throttle rate
exactly at gold's limit (`tests/test_confirmation.py`):

| Procedure | False-PASS rate |
|---|---|
| planned looks, exact bound, `max_looks: 2` | 2.75% (<= 5%) |
| naive peeking after every repetition | 7.0% |

For gold (`max_looks: 2`, per-look 97.5%) the looks are at 3,688 and
5,570 requests: 7 and 10 repetitions at the 6.67 rps ceiling (~600
requests each) -- inside the default caps. In a mix each class's checks
see only that class's requests, so a look is taken when EVERY class has
its own required count -- actual per-class n, not total x expected
share: 6,147 random mix arrivals can hold only 3,600 `short_chat` ones.
The plan records these as `look_requirements` (e.g. look 1: total
3,688, short_chat 3,688, rag_answer 736, long_generation 368). Class
counts depend only on the random class draws, never on outcomes, so the
look times stay outcome-independent and the Bonferroni bound holds.
`look_schedule_requests` (6,147 / 9,284 here) is only the expected total,
used for caps and time estimates -- `mixed-capacity`'s caps are raised
to fit it. More samples, never a looser SLO.

**Caps -> INCONCLUSIVE, never a looser SLO.** `max_repetitions`
(default 10) and `max_requests` (8,000) per candidate, `max_duration_s`
(1,800) for the phase. A candidate whose next look can't be reached
within the caps -- estimated from discovery's requests per repetition --
stops as `unreachable_within_caps` without spending the calls (a gold
candidate at 1.67 rps: ~150 requests/rep, can't reach 3,688 in 10 reps).
Each candidate reports `verdict`, `stop_reason` (`confirmed`,
`observed_violation`, `looks_exhausted`, `max_repetitions`,
`max_requests`, `max_duration`, `unreachable_within_caps`,
`not_tested`), `n`, `looks_used` and `next_look_n` under the subject's
`confirmation` block, next to the `plan` (confidence, per-look
confidence, look schedule, caps).

Only a characterization experiment can run without a confirmation
phase. Then `statistically_confirmed` comes from discovery as a
fixed-sequence test over the sweep's own ascending order
(the top of the leading run of strict PASSes);
`confirmation_source` says which (`confirmation` |
`discovery_fixed_sequence`). That test stops at the first non-PASS
point, and a low point is usually INCONCLUSIVE for gold (one window is
too few requests), so discovery alone rarely confirms anything.

When nothing is confirmed, `recommendation.reason` states the actual
cause -- the first non-PASS confirmation candidate and its
`stop_reason` (with a hint: raise the caps, or test a lower candidate),
or, discovery only, the point the test stopped at with each
`n < required_n`.
