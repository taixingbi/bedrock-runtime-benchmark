# Quota model

> Docs: [methodology](methodology.md) · [SLO statistics](slo-statistics.md) · [quota model](quota-model.md) · [capacity-profile schema](capacity-profile-schema.md) · [experiment design](experiment-design.md) · [correctness history](correctness-history.md) · [README](../README.md)

What the provider allows: the per-model RPM/TPM quota (`constraints/quota.yaml`), the provider ceiling derived from it (including TPM reservation vs consumption), quota-relative rate sweeps, and keeping the quota numbers current.

## Provider ceiling

Rate sweeps are written as `quota_fractions` of each sweep subject's
**provider ceiling** -- the request rate the model's quota allows for
that workload's token shape (`ceiling.py`):

```
rpm_rps = RPM / 60
reservation = input_tokens + max_tokens                         # deducted at admission
consumption = input_tokens + expected_output x output_burndown  # settled at completion
tpm_rps = TPM / max(reservation, consumption) / 60
ceiling = min(rpm_rps, tpm_rps)
```

Quotas cap requests AND tokens, and which binds depends on the
workload (a short request on nova-micro is RPM-bound; a long one on a
tight-TPM model can be TPM-bound). Token pressure has two parts, per
AWS's quota model: at admission Bedrock deducts **input + max_tokens**
(no burndown), and at completion adjusts it to **input + actual output
x burndown**. Reservations are held while requests are in flight and
consumption is what settles, so the ceiling uses the larger. Expected
output is `max_tokens` (the prompts elicit the full budget). With
`output_burndown` 1 -- all current models -- both are input +
max_tokens; with burndown 5 (some Claude models) 4k in / 1k out
reserves 5k but consumes 9k. `output_burndown` is set in
`constraints/quota.yaml` (default 1). Every class/mix in the artifact
records it:

```yaml
provider_constraints:
  tokens_per_request: 576.0         # max(reservation, consumption)
  reservation_tokens: 576.0
  consumption_tokens: 576.0
  token_pressure: reservation       # which of the two binds
  rpm_rps_ceiling: 6.6667
  tpm_rps_ceiling: 231.4815
  ceiling_rps: 6.6667
  binding_constraint: rpm      # or tpm
sweep_values_rps: [1.6667, 3.3333, ...]
```

Quotas differ by an order of magnitude (50 RPM for nova-pro, 1000 for
qwen3-32b), so the same `[0.25 .. 2.5]` sweep is 0.21-2.08 rps on
nova-pro and 4.2-41.7 rps on qwen3-32b. Concurrency sweeps stay
absolute (and may not exceed `transport.max_connections`).

## Quota-aware experiment design

Picking sane sweep values (especially rate-sweep values) is guesswork
without knowing the model's real RPM/TPM ceiling first -- a
concurrency sweep starting at `[1, 2, 4, ...]` is useless if even
concurrency=1 closed-loop already runs over quota, which turns out to
be true for some certified models. `scripts/fetch_quota.py` and
`src/bedrock_benchmark/quota.py` exist to make that ceiling a known
number before an experiment is written, not a name for it to just do.

```bash
.venv/bin/python scripts/fetch_quota.py --all                                 # check constraints/quota.yaml (live account)
.venv/bin/python scripts/fetch_quota.py --model-id us.amazon.nova-pro-v1:0    # one model, for a new entry
```

`--all` compares each entry in `constraints/quota.yaml` with the live
value and prints a replacement line for any stale one (exit 1 if any
differ). It looks up the model's real RPM/TPM in two steps, table first:

1. `gateway-model-quotas-dev`'s `quota#<model_id>` row, if that table
   happens to be reachable -- a cheap `GetItem` against a value
   `bedrock-runtime-gateway` already synced from AWS. A soft
   convenience: this repo doesn't provision that table and doesn't
   assume it exists.
2. AWS Service Quotas directly (`service-quotas:ListServiceQuotas`),
   the actual source of truth, whenever the table lookup fails for
   any reason (table missing, row missing, no permission, wrong
   account/region).

If neither source is available it returns `source: unknown` rather
than raising -- a missing quota number should never block an
experiment design conversation, it should just make the gap visible.
This is read-only, design-time context: nothing at runtime checks or
caps against it -- enforcement is the gateway's job.

Why rate sweeps are quota-relative: nova-pro's 50 RPM (0.83 rps) with
~616ms latency means concurrency=1 closed-loop already runs ~2x over
quota, and llama3-3-70b's 80 RPM sits right at its C=1 rate -- a
concurrency sweep can't resolve either inference profile's safe zone, and a fixed
rps list tuned for one model is useless for another. The first full
batch found real ceilings between ~1.0x and ~1.9x quota, so the shipped
sweeps span 0.25x-2.5x.
