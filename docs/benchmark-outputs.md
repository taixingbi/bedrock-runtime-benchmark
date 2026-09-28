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

A gateway consumes them -- after temporal validation (below), not from
a single run:

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
| `C_saturation` | `concurrency.saturation.observed_edge` | Concurrency where the DISCOVERY sweep first saw an SLO violation -- an observed edge, not statistically confirmed |
| `R_saturation` | `rate.saturation.observed_edge` | Offered rate where the DISCOVERY sweep first saw an SLO violation -- an observed edge, not statistically confirmed |

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
| `production_capacity_input` | The conservative (minimum) admission value, set only once there is enough temporal evidence |

A single run is a `single_run_operating_envelope`, not a production-safe
config:

```text
single run                         -> single_run_operating_envelope (every profile)
    ↓
repeated runs across times / days
    ↓
temporal validation                -> bedrock-benchmark validate: temporal-capacity-profile.yaml
    ↓
conservative / stable envelope     -> the minimum confirmed admission value
    ↓
production capacity input          -> temporal_validation.production_capacity_input
```

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

> **`C_admission` and `R_admission` configure admission control -- once temporally validated; the remaining benchmark outputs provide the evidence, scope, constraints, and validity of those limits.**

They are two independently confirmed guardrails, not a jointly validated
2-D (C, R) safe region -- see [admission control](admission-control.md#two-guardrails-not-a-2-d-safe-region).