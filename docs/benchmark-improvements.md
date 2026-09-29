# Benchmark improvements

## P0: correctness and fresh measurements

- A workload with `workload_validation.valid: false` receives no capacity
  recommendation or admission calibration point. A mix is blocked if any
  participating workload fails validation.
- Temporal validation excludes invalid workloads from historical files as
  well as new reports. The old concurrency run 467c8026 measured 266 input
  tokens for short_chat (target 512), and 1701 for rag_answer (target 4096).
  Its concurrency results are historical observations, not production references.
- SDK execution uses a locked live counter, including stream consumption.
  If its peak exceeds configured closed-loop concurrency, the subject becomes
  measurement-invalid and receives no recommendation. The actual counter is
  recorded separately from timestamp-reconstructed outstanding metrics.
- Rerun the reference concurrency experiment with token calibration and
  these gates to obtain each workload's C_safe. New values require completed,
  valid measurements; old values cannot substitute.

## P1: measurement protocol

- Reference rate measures sustainable RPS and stops increasing load at a clear
  failure. Higher overload fractions belong to the separate burst experiment.
- Recovery requires both liveness and baseline-capacity probes, checking
  throttling, goodput and latency.
- Confirmation requires statistical PASS and at least 300 seconds of steady
  load. Fresh confirmation data stays separate from discovery and refinement.
- Concurrency retains failure-based early stopping, integer refinement and
  fresh confirmation.
- Repeated runs rotate workload order: short/rag/long, rag/long/short,
  long/short/rag.

## Experiment responsibilities

| Experiment | Result |
| --- | --- |
| capacity-reference-concurrency | Isolated safe max_inflight |
| capacity-reference-rate | Safe sustained_rps |
| capacity-burst-rate | Overload, burst and observed recovery |
| capacity-shape-concurrency | Workload-shape effects |
| Temporal validation | Capacity envelope supported across runs and times |

Per-workload concurrency limits are not additive. A single run remains a
snapshot; production inputs require temporal validation.
