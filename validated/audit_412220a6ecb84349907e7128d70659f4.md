Audit Report

## Title
Wrong Field Assigned to `max_neurons_fund_participation_icp_e8s` in `From` Conversion - (File: rs/sns/swap/src/neurons_fund.rs)

## Summary
In the `From<ValidatedNeuronsFundParticipationConstraints<F>> for NeuronsFundParticipationConstraintsPb` implementation at lines 502–527 of `rs/sns/swap/src/neurons_fund.rs`, the `max_neurons_fund_participation_icp_e8s` field is populated with `value.min_direct_participation_threshold_icp_e8s` instead of `value.max_neurons_fund_participation_icp_e8s`. This copy-paste error causes the serialized protobuf to carry an incorrect Neurons' Fund participation cap, which can cause SNS swaps to abort due to under-matching or allow the Neurons' Fund to exceed its governance-approved contribution ceiling.

## Finding Description
At lines 512–514 of `rs/sns/swap/src/neurons_fund.rs`, the `From` conversion reads:

```rust
max_neurons_fund_participation_icp_e8s: Some(
    value.min_direct_participation_threshold_icp_e8s,  // BUG
),
```

instead of:

```rust
max_neurons_fund_participation_icp_e8s: Some(
    value.max_neurons_fund_participation_icp_e8s,
),
```

These two fields have entirely different semantics: `min_direct_participation_threshold_icp_e8s` is the floor below which the Neurons' Fund does not participate at all, while `max_neurons_fund_participation_icp_e8s` is the absolute cap on Neurons' Fund contributions. This `From` impl is the only serialization path from `ValidatedNeuronsFundParticipationConstraints` back to `NeuronsFundParticipationConstraintsPb`. Any code path that round-trips through this conversion (storage, inter-canister response, downstream logic) will produce a corrupted protobuf. The upstream validation in `rs/sns/init/src/lib.rs` (lines 1819–1845) operates on the incoming protobuf from NNS Governance, not on the re-serialized output of this conversion, so the corrupted value bypasses all existing guards.

## Impact Explanation
This is a **High** severity finding. The corrupted `max_neurons_fund_participation_icp_e8s` value directly affects SNS swap execution: in the typical case where `min_direct_participation_threshold_icp_e8s` (e.g., 50 ICP) is far below `max_neurons_fund_participation_icp_e8s` (e.g., 200,000 ICP), the Neurons' Fund participation is capped at 50 ICP instead of 200,000 ICP. This causes swaps that depend on Neurons' Fund matched funding to fail to reach `min_participants` or `min_direct_participation_icp_e8s`, aborting the SNS launch and refunding all participants. In the reverse case, the Neurons' Fund is permitted to contribute beyond its governance-approved maximum, violating the conservation invariant enforced by NNS Governance. This maps to: *Significant SNS security impact with concrete user or protocol harm* (High, $2,000–$10,000).

## Likelihood Explanation
Every SNS swap that uses Neurons' Fund matched funding and exercises the round-trip through this `From` conversion will be affected. No special privileges are required — any user triggering an SNS swap with Neurons' Fund participation enabled causes this path to execute. The bug is deterministic and reproducible on every affected swap.

## Recommendation
Fix the copy-paste error at line 513 of `rs/sns/swap/src/neurons_fund.rs`:

```rust
max_neurons_fund_participation_icp_e8s: Some(
    value.max_neurons_fund_participation_icp_e8s,  // was: value.min_direct_participation_threshold_icp_e8s
),
```

Add a unit test that constructs a `ValidatedNeuronsFundParticipationConstraints` with distinct values for both fields, round-trips it through `From`, and asserts both output fields are correct.

## Proof of Concept
Given:
- `min_direct_participation_threshold_icp_e8s = 50 * E8`
- `max_neurons_fund_participation_icp_e8s = 200_000 * E8`

After the `From` conversion at lines 502–527:
- `output.min_direct_participation_threshold_icp_e8s = 50 * E8` ✓
- `output.max_neurons_fund_participation_icp_e8s = 50 * E8` ✗ (should be `200_000 * E8`)

A unit test constructing this struct, calling `.into::<NeuronsFundParticipationConstraintsPb>()`, and asserting `output.max_neurons_fund_participation_icp_e8s == Some(200_000 * E8)` will fail, confirming the bug. [1](#0-0)

### Citations

**File:** rs/sns/swap/src/neurons_fund.rs (L502-527)
```rust
impl<F> From<ValidatedNeuronsFundParticipationConstraints<F>>
    for NeuronsFundParticipationConstraintsPb
where
    F: IdealMatchingFunction,
{
    fn from(value: ValidatedNeuronsFundParticipationConstraints<F>) -> Self {
        Self {
            min_direct_participation_threshold_icp_e8s: Some(
                value.min_direct_participation_threshold_icp_e8s,
            ),
            max_neurons_fund_participation_icp_e8s: Some(
                value.min_direct_participation_threshold_icp_e8s,
            ),
            coefficient_intervals: value
                .coefficient_intervals
                .into_iter()
                .map(LinearScalingCoefficientPb::from)
                .collect(),
            ideal_matched_participation_function: Some(IdealMatchedParticipationFunctionSwapPb {
                serialized_representation: Some(
                    value.ideal_matched_participation_function.serialize(),
                ),
            }),
        }
    }
}
```
