Audit Report

## Title
Wrong Field Assignment in `From<ValidatedNeuronsFundParticipationConstraints>` Sets Incorrect Neurons' Fund Participation Cap — (File: `rs/sns/swap/src/neurons_fund.rs`)

## Summary
At line 513 of `rs/sns/swap/src/neurons_fund.rs`, the `From<ValidatedNeuronsFundParticipationConstraints<F>> for NeuronsFundParticipationConstraintsPb` implementation assigns `value.min_direct_participation_threshold_icp_e8s` to the `max_neurons_fund_participation_icp_e8s` field of the output protobuf, instead of the correct `value.max_neurons_fund_participation_icp_e8s`. Every SNS swap that uses Neurons' Fund matched funding serializes constraints through this path, causing the hard cap to be silently replaced with the minimum threshold value. Downstream deserialization reconstructs a `ValidatedNeuronsFundParticipationConstraints` with the wrong cap, causing the Neurons' Fund to contribute incorrect amounts during swap finalization.

## Finding Description
The bug is confirmed at `rs/sns/swap/src/neurons_fund.rs` lines 502–527:

```rust
impl<F> From<ValidatedNeuronsFundParticipationConstraints<F>>
    for NeuronsFundParticipationConstraintsPb
{
    fn from(value: ValidatedNeuronsFundParticipationConstraints<F>) -> Self {
        Self {
            min_direct_participation_threshold_icp_e8s: Some(
                value.min_direct_participation_threshold_icp_e8s,  // correct
            ),
            max_neurons_fund_participation_icp_e8s: Some(
                value.min_direct_participation_threshold_icp_e8s,  // BUG: copies min into max
            ),
            ...
        }
    }
}
``` [1](#0-0) 

The two fields are semantically distinct: `min_direct_participation_threshold_icp_e8s` is the floor below which the Neurons' Fund does not participate at all, while `max_neurons_fund_participation_icp_e8s` is the hard cap enforced on every call to `MatchedParticipationFunction::apply`. The `TryFrom<&NeuronsFundParticipationConstraintsPb>` inverse conversion correctly reads both fields independently from the protobuf: [2](#0-1) 

When the corrupted protobuf is deserialized, the reconstructed `ValidatedNeuronsFundParticipationConstraints` carries `min_direct_participation_threshold_icp_e8s` in the `max_neurons_fund_participation_icp_e8s` slot. No validation error is raised because the value is a valid `u64`. The hard cap is then enforced at the wrong level on every invocation of the matched participation function during swap finalization.

## Impact Explanation
`max_neurons_fund_participation_icp_e8s` is the hard cap applied in `MatchedParticipationFunction::apply` (in `rs/nervous_system/neurons_fund/src/lib.rs`) on every computation of Neurons' Fund contribution during SNS swap finalization. With the wrong cap in place:

- **Typical case** (`min_threshold < max_cap`, e.g., 36,000 ICP vs 45,000 ICP): the cap is set 9,000 ICP too low. The Neurons' Fund contributes less than governance approved, potentially causing a swap to fall short of its minimum ICP target and fail — a concrete SNS governance/financial harm.
- **Atypical case** (`min_threshold > max_cap`): the cap is set too high, allowing the Neurons' Fund to contribute more ICP than the NNS-approved maximum, constituting unauthorized over-allocation of Neurons' Fund maturity.

This is a significant SNS security impact with concrete user and protocol harm, qualifying as **High ($2,000–$10,000)** under "Significant SNS security impact with concrete user or protocol harm."

## Likelihood Explanation
The `From` conversion is production code exercised for every SNS swap with `neurons_fund_participation = true`. No special attacker action is required — the bug fires automatically during normal NNS governance execution of any Neurons' Fund-enabled SNS swap. The corruption is silent: no error is returned, no trap is triggered, and the wrong value passes all downstream validation. The bug affects every such swap unconditionally.

## Recommendation
Change line 513 of `rs/sns/swap/src/neurons_fund.rs` to use the correct source field:

```rust
max_neurons_fund_participation_icp_e8s: Some(
    value.max_neurons_fund_participation_icp_e8s,  // was: value.min_direct_participation_threshold_icp_e8s
),
```

Add a round-trip test constructing a `ValidatedNeuronsFundParticipationConstraints` with distinct values for both fields, converting via `From` to `NeuronsFundParticipationConstraintsPb`, and asserting each output field matches its respective source field.

## Proof of Concept
Using the test data from `rs/nns/governance/src/governance/test_data.rs` where `min_direct_participation_threshold_icp_e8s = 36_000 * E8` and `max_neurons_fund_participation_icp_e8s = 45_000 * E8`:

1. Construct a `ValidatedNeuronsFundParticipationConstraints` with these distinct values.
2. Call `NeuronsFundParticipationConstraintsPb::from(constraints)`.
3. Observe: `max_neurons_fund_participation_icp_e8s` in the output is `36_000 * E8` (wrong) instead of `45_000 * E8`.
4. Deserialize via `TryFrom<&NeuronsFundParticipationConstraintsPb>` — the reconstructed struct has `max_neurons_fund_participation_icp_e8s = 36_000 * E8`.
5. Call `MatchedParticipationFunction::apply` — the hard cap is enforced at 36,000 ICP instead of 45,000 ICP, silently reducing Neurons' Fund contribution by up to 9,000 ICP per swap. [3](#0-2)

### Citations

**File:** rs/sns/swap/src/neurons_fund.rs (L302-326)
```rust
impl<F> TryFrom<&NeuronsFundParticipationConstraintsPb>
    for ValidatedNeuronsFundParticipationConstraints<F>
where
    F: DeserializableFunction,
{
    type Error = NeuronsFundParticipationConstraintsValidationError;

    fn try_from(value: &NeuronsFundParticipationConstraintsPb) -> Result<Self, Self::Error> {
        // Validate min_direct_participation_threshold_icp_e8s
        let min_direct_participation_threshold_icp_e8s = value
            .min_direct_participation_threshold_icp_e8s
            .ok_or_else(|| {
                Self::Error::RelatedFieldUnspecified(
                    "min_direct_participation_threshold_icp_e8s".to_string(),
                )
            })?;

        // Validate max_neurons_fund_participation_icp_e8s
        let max_neurons_fund_participation_icp_e8s = value
            .max_neurons_fund_participation_icp_e8s
            .ok_or_else(|| {
            Self::Error::RelatedFieldUnspecified(
                "max_neurons_fund_participation_icp_e8s".to_string(),
            )
        })?;
```

**File:** rs/sns/swap/src/neurons_fund.rs (L507-514)
```rust
    fn from(value: ValidatedNeuronsFundParticipationConstraints<F>) -> Self {
        Self {
            min_direct_participation_threshold_icp_e8s: Some(
                value.min_direct_participation_threshold_icp_e8s,
            ),
            max_neurons_fund_participation_icp_e8s: Some(
                value.min_direct_participation_threshold_icp_e8s,
            ),
```
