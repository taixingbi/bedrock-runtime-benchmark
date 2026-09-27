## Minimum Admission Control Configuration

For a single shared Bedrock backend with no multi-tenant isolation, the minimum production admission-control configuration is:

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