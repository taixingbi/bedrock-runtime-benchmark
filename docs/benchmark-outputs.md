## Benchmark Outputs

The benchmark produces two types of outputs:

1. **Admission-control configuration** that can be consumed by a production gateway.
2. **Evidence and metadata** that explain why the recommended limits are valid.

### Production Admission-Control Outputs

| Name | Output | Purpose |
|---|---|---|
| `C_admission` | `max_inflight` | Recommended maximum number of concurrent in-flight requests |
| `R_admission` | `sustained_rps` | Recommended sustainable request rate |

These are the primary production outputs:

```text id="o8g8xu"
C_confirmed
    ↓ safety headroom
C_admission
    ↓
max_inflight


R_confirmed
    ↓ safety + quota headroom
R_admission
    ↓
sustained_rps
```

A gateway can consume them directly:

```yaml id="hfp5s3"
admission_control:
  max_inflight: 4
  sustained_rps: 5.33
```

### Capacity Measurement Outputs

| Name | Output | Meaning |
|---|---|---|
| `C_confirmed` | `statistically_confirmed_concurrency` | Highest statistically confirmed SLO-compliant concurrency |
| `R_confirmed` | `statistically_confirmed_offered_rps` | Highest statistically confirmed SLO-compliant offered request rate |
| `C_saturation` | concurrency saturation point | Concurrency where the workload begins to violate the SLO or overload the backend |
| `R_saturation` | `saturation_offered_rps` | Offered rate where the workload begins to violate the SLO or overload the backend |

### SLO Evidence

| Name | Meaning |
|---|---|
| `TTFT_p95` | P95 time to first token |
| `TPOT_p95` | P95 time per output token |
| `E2E_p95` | P95 end-to-end latency |
| `success_rate` | Fraction of successful requests |
| `throttle_rate` | Fraction of provider-throttled requests |
| `SLO_goodput` | Throughput delivered while satisfying the SLO |
| `verdict` | `PASS`, `FAIL`, or `INCONCLUSIVE` |

### Provider Constraint Outputs

| Name | Meaning |
|---|---|
| `provider_rpm` | Provider requests-per-minute quota |
| `provider_tpm` | Provider tokens-per-minute quota |
| `provider_ceiling_rps` | Quota-derived sustainable request-rate ceiling |
| `binding_constraint` | Constraint currently limiting capacity, such as RPM, TPM, latency, or throttling |

These outputs help distinguish backend saturation from provider-quota saturation.

### Workload Outputs

| Name | Meaning |
|---|---|
| `workload_class` | Input/output token shape being measured |
| `input_tokens` | Target input-token count |
| `output_tokens` | Maximum output-token count |
| `slo_profile` | SLO policy applied to the workload |
| `workload_validation` | Evidence that the actual request shape matches the intended workload |

### Mixed-Workload Outputs

| Name | Meaning |
|---|---|
| `mix` | Traffic composition, such as 60% chat / 30% RAG / 10% long generation |
| `R_mix_confirmed` | Statistically confirmed sustainable offered rate for that workload mix |
| `R_mix_admission` | Recommended production rate for that mix after headroom |
| `per_class_verdict` | SLO result for each workload class within the mix |

### Temporal Validation Outputs

| Name | Meaning |
|---|---|
| `capacity_min` | Minimum confirmed capacity across repeated runs |
| `capacity_median` | Median confirmed capacity |
| `capacity_max` | Maximum confirmed capacity |
| `capacity_spread` | Variation across repeated measurements |
| `temporal_status` | Whether the measured envelope is stable, unstable, or lacks enough temporal evidence |

The overall output can therefore be viewed as:

```text id="osts6u"
Benchmark
   |
   +-- Capacity
   |     C_confirmed
   |     R_confirmed
   |     saturation points
   |
   +-- SLO Evidence
   |     TTFT / TPOT / E2E
   |     success / throttle
   |     PASS / FAIL / INCONCLUSIVE
   |
   +-- Provider Constraints
   |     RPM / TPM
   |     provider ceiling
   |     binding constraint
   |
   +-- Workload Evidence
   |     token shape
   |     workload validation
   |
   +-- Stability
   |     temporal validation
   |
   +-- Recommendation
         C_admission → max_inflight
         R_admission → sustained_rps
```

The primary production contract is:

> **`C_admission` and `R_admission` configure admission control; the remaining benchmark outputs provide the evidence, scope, constraints, and validity of those limits.**