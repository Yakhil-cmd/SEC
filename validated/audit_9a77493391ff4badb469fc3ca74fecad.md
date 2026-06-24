Audit Report

## Title
Wrong Field Assigned to `max_neurons_fund_participation_icp_e8s` in `From` Conversion — (File: rs/sns/swap/src/neurons_fund.rs)

## Summary
In `rs/sns/swap/src/neurons_fund.rs` at lines 512–514, the `From<ValidatedNeuronsFundParticipationConstraints<F>> for NeuronsFundParticipationConstraintsPb` impl assigns `value.min_direct_participation_threshold_icp_e8s` to the `max_neurons_fund_participation_icp_e8s` field instead of `value.max_neurons_fund_participation_icp_e8s`. This silently sets the hard cap on Neurons' Fund ICP disbursement to the minimum participation threshold rather than the actual maximum, causing every SNS swap that exercises this conversion path to under-disburse Neurons' Fund ICP.

## Finding Description
The confirmed buggy assignment is at lines 512–514:

```rust
max_neurons_fund_participation_icp_e8s: Some(
    value.min_direct_participation_threshold_icp_e8s,  // copies min, not max
),
```

The correct source field `value.max_neurons_fund_participation_icp_e8s` is populated by the `TryFrom` path (lines 416–421) but is silently discarded in the reverse `From` direction. The `max_neurons_fund_participation_icp_e8s` field is consumed at line 1025 of `rs/nervous_system/neurons_fund/src/lib.rs` as `hard_cap_icp`, which is the sole ceiling applied to Neurons' Fund ICP disbursement in `MatchedParticipationFunction::apply`:

```rust
let hard_cap_icp = rescale_to_icp(self.max_neurons_fund_participation_icp_e8s)?;
let effective_icp = hard_cap_icp.min(intercept_icp.saturating_add(...));
```

When the `From` impl is invoked, `hard_cap_icp` is set to `min_direct_participation_threshold_icp_e8s` (e.g., 36,000 ICP) instead of the actual maximum (e.g., 45,000 ICP), causing the SNS Swap canister to enforce a lower ceiling than was computed and intended by NNS governance.

## Impact Explanation
This is a significant SNS financial impact with concrete protocol harm. The Neurons' Fund hard cap is silently set to the wrong (lower) value, causing under-disbursement of Neurons' Fund ICP in every affected SNS swap. SNS swaps that depend on Neurons' Fund participation to reach their minimum ICP target may fail. Neurons' Fund maturity is reserved by NNS governance for a swap but the swap canister caps disbursement at the wrong value, constituting an accounting inconsistency in the SNS/NNS financial flow. This matches the allowed impact: "Significant SNS security impact with concrete user or protocol harm" — **High ($2,000–$10,000)**.

## Likelihood Explanation
Any `CreateServiceNervousSystem` proposal adopted by the NNS with `neurons_fund_participation: true` that exercises the `From<ValidatedNeuronsFundParticipationConstraints<F>> for NeuronsFundParticipationConstraintsPb` conversion is affected. No special privileges, key compromise, or threshold attack is required beyond normal NNS governance participation. The conversion is a standard Rust `From`/`Into` impl in the production SNS Swap crate.

## Recommendation
Fix the field assignment in the `From` impl at line 513 of `rs/sns/swap/src/neurons_fund.rs`:

```rust
max_neurons_fund_participation_icp_e8s: Some(
    value.max_neurons_fund_participation_icp_e8s,  // correct field
),
```

Add a round-trip test that constructs a `ValidatedNeuronsFundParticipationConstraints` with distinct `min_direct_participation_threshold_icp_e8s` (e.g., 36,000 × E8) and `max_neurons_fund_participation_icp_e8s` (e.g., 45,000 × E8), converts to `NeuronsFundParticipationConstraintsPb` via `From`, and asserts that `max_neurons_fund_participation_icp_e8s` in the output equals 45,000 × E8, not 36,000 × E8.

## Proof of Concept
Given a `ValidatedNeuronsFundParticipationConstraints` with:
- `min_direct_participation_threshold_icp_e8s = 36_000 * E8`
- `max_neurons_fund_participation_icp_e8s = 45_000 * E8`

(matching test data in `rs/nns/governance/src/governance/test_data.rs` lines 205–233)

After `NeuronsFundParticipationConstraintsPb::from(validated)`:

```
result.max_neurons_fund_participation_icp_e8s == Some(36_000 * E8)  // actual (wrong)
// should be:
result.max_neurons_fund_participation_icp_e8s == Some(45_000 * E8)  // expected (correct)
```

The SNS Swap canister then calls `apply(direct_participation)` and caps Neurons' Fund ICP at 36,000 ICP instead of 45,000 ICP, silently under-disbursing up to 9,000 ICP per swap from the Neurons' Fund.