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

Isolated workloads are gated on their own profile. In a mix, every
request counts toward goodput against its own class's profile and
**a mixed point PASSes exactly when every class PASSes its own profile**
(`short_chat` -> gold, `rag_answer` -> silver, `long_generation` ->
bronze). The blend -- aggregate success, throttle, TTFT, throughput,
goodput -- is reported but never gated: a blend gate at the strictest
class's limit would add a constraint no class has (60% at 99.5% + 40% at
99.0% blend to ~99.3%, which would FAIL a 99.5% blend gate while every
class meets its SLO). A mix-level SLO would be a separate, explicit
business policy -- none is defined.

**One population per SLO kind**, the same for the reported value and the
statistical gate: success and throttle over every request; latency,
TTFT and TPOT (reported percentiles and exceedance tests alike) over
successful requests only -- a fast 429 is not a latency measurement.
Rate decisions use exact counts (`n_success / n`, `n_throttled / n`);
the 4-decimal rates in the profile are for reading, never deciding. Resolving a throttle limit statistically
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
coarse sweep              1 2 4 6 8 12(FAIL)
      |
non-FAIL / FAIL bracket   lower = highest leading non-FAIL (PASS or INCONCLUSIVE) = 8
      |                   upper = first FAIL = 12
integer refinement        10, then 11 or 9 -- until adjacent
      |
candidate selection       the top non-FAIL points (e.g. 10, 11)
      |
fresh confirmation        independent reps, pre-planned looks
      |
statistically confirmed   the capacity
      |
headroom                  max_inflight = floor(confirmed x (1 - headroom))
```

The bracket's lower bound only needs to be non-FAIL: discovery picks
candidates, it never proves anything, so an INCONCLUSIVE point is a
valid lower bound. Only a FAIL (an observed violation) is an upper
bound.

| Phase | Data | Used for | Never used for |
|---|---|---|---|
| **discovery** | every sweep value, `repetitions` each | observed verdicts, saturation, transition region, **choosing candidates** | confirming anything |
| **refinement** (concurrency, `sweep.refinement`) | bisection between the last non-failing point L and the first FAIL F | moving the bracket to the real edge, so candidates aren't stuck at the coarse grid point below it | confirming anything |
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
run, at or below the provider ceiling -- for a rate sweep its offered
rps, for a concurrency sweep the request rate it ACHIEVED (more than 10%
over the ceiling = burst). Above the ceiling a point passes discovery on
burst allowance only: a concurrency that needs 9 rps can't hold under a
6.67 rps quota, and confirming it just measures the bucket draining (the
first `workload-shape-calibration` run: every candidate was at 8.3-9.1
rps and throttled 64-81% in confirmation). Refinement treats an
above-ceiling point as the upper bound of its bracket too.

**Steady state before the looks.** `confirmation.cooldown_s` idles after
discovery (its overload drains the provider's burst / rolling quota), and
`confirmation.warmup_s` then runs load at each candidate with the data
DISCARDED (`phase: conditioning`, never measured) -- a rested bucket
hands out burst credit at first, which fixed-N looks must not read.
Neither counts against `max_duration_s`.

**Several candidates: highest first, alpha split.** With `candidates: K
> 1` they're tested HIGHEST first and stop at the first PASS; a FAIL (or
cap) moves on to the next lower one. The goal is the maximum confirmed
point, and the highest usually confirms -- so this avoids confirming a
lower point on the way up. But every candidate is another chance of a
false PASS ("the high one falsely passes", or "it fails, then the low
one falsely passes"), so alpha is split over candidates AND looks: each
look runs at `1 - alpha / (max_looks x K)` (Bonferroni), which proves
the procedure's false-PASS rate <= alpha. K is the number of candidates
discovery actually chose -- known before any confirmation data -- so a
single candidate keeps `1 - alpha / max_looks`.

| Tier | Looks, K = 1 | Looks, K = 2 |
|---|---|---|
| gold | 3,688 / 5,570 | 4,380 / 6,379 |
| silver | 736 / 1,113 | 875 / 1,274 |
| bronze | 368 / 555 | 437 / 636 |

Versus lowest-first (a fixed sequence that keeps alpha unsplit but
confirms every lower point first): if the high candidate PASSes it costs
4,380 requests instead of 2 x 3,688; if it FAILs -- usually within one
repetition -- the lower one costs 4,380 instead of 3,688. Simulated with
both candidates exactly at gold's limit (`tests/test_confirmation.py`):
2.8% false PASS with the split; ~4.8% at full alpha per candidate, whose
proven bound is only 10%.
`cooldown_s` idles between the phases so discovery's overload (the
saturation point is the last one swept) doesn't bleed into the first
confirmation repetition; it isn't counted against `max_duration_s`.

**Adaptive, but no peeking.** Repetitions are added one at a time, but
a PASS can only be declared at `max_looks` sample sizes fixed before any
confirmation data exists -- look j is where j-1 bad events would still
clear the limit -- each at confidence `1 - 0.05 / max_looks`
(Bonferroni). FAIL (an observed violation) stops it at any time.

**Fixed-count looks.** Look j is decided on EXACTLY the first N_j
confirmation requests by scheduled time (in a mix, the first N_c,j of
each class) -- not on however many a 120 s repetition produced. In a
closed-loop concurrency run the count per repetition depends on
outcomes (a 429 returns in milliseconds and frees the worker for another
request; slow responses mean fewer), so "evaluate all n >= N_j" would
make the tested sample size outcome-dependent. Truncating to N_j keeps
every look an exact binomial test at a pre-declared n. Each candidate
reports `decision: {n, n_throttled, throttle_rate_upper,
success_rate_lower}` -- the sample the look was decided on -- next to
the requests collected. Simulated with outcome-dependent counts (each
throttle adds 19 requests to its repetition), 4,000 trials at a true
rate exactly at gold's limit: 3.8% false PASS with fixed-count looks
(<= 5% guaranteed by construction); `tests/test_confirmation.py` checks
<= 5%.
Checking the bound after every repetition and stopping on the first
clear would inflate false PASSes; simulated at a true throttle rate
exactly at gold's limit (`tests/test_confirmation.py`):

| Procedure | False-PASS rate |
|---|---|
| planned looks, exact bound, `max_looks: 2` | 2.75% (<= 5%) |
| naive peeking after every collection window | 7.0% |

For gold (`max_looks: 2`, per-look 97.5%) the looks are at N = 3,688
and 5,570 requests (at the 6.67 rps ceiling, ~560 s and ~840 s of
windows -- a time estimate, not part of the rule). In a mix each class's checks
see only that class's requests, so a look is taken when EVERY class has
its own required count -- actual per-class n, not total x expected
share: 6,147 random mix arrivals can hold only 3,600 `short_chat` ones.
The plan records these as `look_requirements` (e.g. look 1:
short_chat 3,688, rag_answer 736, long_generation 368 -- per class
only, since the blend isn't gated). Class
counts depend only on the random class draws, never on outcomes, so the
look times stay outcome-independent and the Bonferroni bound holds.
`look_schedule_requests` (6,147 / 9,284 here) is only the expected total,
used for caps and time estimates -- `mixed-capacity`'s `max_requests` is
raised to fit it. More samples, never a looser SLO.

**Stopping rule.** Three concepts, kept apart:

| | Is |
|---|---|
| **request** | the statistical unit |
| **look** | when a statistical decision is allowed (pre-planned N) |
| **repetition** | how data is collected -- a window of load, nothing more |

A candidate stops only on:

1. a pre-planned sample-count look PASSes -> **PASS**
2. an observed violation (any time) -> **FAIL**
3. `max_requests` reached -> **INCONCLUSIVE**
4. `max_duration_s` reached -> **INCONCLUSIVE**

Never on a number of repetitions: no shipped experiment sets
`max_repetitions` (it stays in the schema, default none, as an optional
cost guardrail for special experiments). "N repetitions" appears in
these docs only as a time estimate. A
candidate whose next look can't be reached within the caps -- estimated
from discovery's requests per repetition -- stops as
`unreachable_within_caps` without spending the calls (a gold candidate
at 1.67 rps collects ~150 requests per 90 s window: 3,688 doesn't fit in
1,800 s).
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
