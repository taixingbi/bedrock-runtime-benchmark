## Minimum Admission Control Configuration

For a single shared Bedrock backend with no multi-tenant isolation, the
minimum production admission-control configuration has the shape below
(values are illustrative). The two capacity values must come from a
temporally validated conservative envelope -- not from a single
benchmark run (see [Production capacity input](#production-capacity-input)):

```yaml
admission_control:
  max_inflight: 4
  sustained_rps: 5.33

  burst_capacity: 8

  overload_action: reject
  queue_max_wait_ms: 0

  request_timeout_ms: 30000

  capacity_profile_version: nova-micro-2026-09-27
```

| Config | Required | Source | Purpose |
|---|---|---|---|
| `max_inflight` | Yes | Benchmark `C_admission` | Maximum number of concurrent in-flight requests allowed to the backend |
| `sustained_rps` | Yes | Benchmark `R_admission` | Sustainable request rate allowed to the backend |
| `burst_capacity` | Yes for token-bucket rate limiting | Gateway policy | Allows bounded short-term bursts above the sustained rate |
| `overload_action` | Yes | Gateway policy | Defines whether excess requests are rejected or queued |
| `queue_max_wait_ms` | Required when queueing is enabled | Gateway policy | Maximum time an admitted request may wait for capacity |
| `request_timeout_ms` | Yes | Gateway policy | Prevents requests from holding concurrency capacity indefinitely |
| `capacity_profile_version` | Recommended for production | Benchmark artifact | Records which benchmark result produced the active capacity settings |

The two capacity settings produced directly from the benchmark are:

```text
C_confirmed
    ↓ safety headroom
C_admission
    ↓
max_inflight


R_confirmed
    ↓ safety / quota headroom
R_admission
    ↓
sustained_rps
```

The remaining settings define how the gateway behaves when incoming load exceeds the measured admission envelope.

For a fail-fast configuration:

```yaml
overload_action: reject
queue_max_wait_ms: 0
```

The benchmark determines the backend capacity envelope; the gateway enforces that envelope at runtime.

### Two guardrails, not a 2-D safe region

`max_inflight` and `sustained_rps` are two **independently confirmed
guardrails**:

- `concurrency-sweep` controls concurrency C and lets the rate emerge;
- `rate-capacity` controls offered rate R and lets concurrency emerge.

Neither experiment tested (C, R) combinations, so the benchmark has
**not** shown that `C <= max_inflight AND R <= sustained_rps` is safe as
a combination -- it is not a jointly validated 2-D safe operating
region. Enforcing both is conservative in practice, but it is an
engineering judgment, not a statistical result. Validating (C, R)
points jointly is the planned `joint-capacity` experiment (not built;
see [experiment design](experiment-design.md#planned-joint-capacity)).

### Production capacity input

A production capacity input must be statistically confirmed, from a
measurement whose `measurement_validity` is `valid`, and temporally
validated -- anything else is evidence, not config.

A single benchmark run produces a `single_run_operating_envelope` -- one
snapshot of provider conditions at `measured_at`. Every admission
envelope in a profile says so (`evidence: single_run_operating_envelope`),
and it is **not** by itself a production-safe config. The path to a
production value:

```text
single run                         -> single_run_operating_envelope (every profile)
    ↓
repeated runs across times / days
    ↓
temporal validation                -> scripts/drift.py: temporal_validation
    ↓
conservative / stable envelope     -> the minimum confirmed admission value
    ↓
production capacity input          -> temporal_validation.production_capacity_input
```