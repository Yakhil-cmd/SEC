The bug is confirmed at lines 512–513. The `From` implementation assigns `value.min_direct_participation_threshold_icp_e8s` to both fields instead of using `value.max_neurons_fund_participation_icp_e8s` for the cap field.

Audit Report

## Title
Wrong Field Assignment in `From<ValidatedNeuronsFundParticipationConstraints>` Sets Incorrect Neurons' Fund Participation Cap — (File: `rs/sns/swap/src/neurons_fund.rs`)

## Summary
In `rs/sns/swap/src/neurons_fund.rs`, the `From<ValidatedNeuronsFundParticipationConstraints<F>> for NeuronsFundParticipationConstraintsPb` implementation at line 513 assigns `value.min_direct_participation_threshold_icp_e8s` to the `max_neurons_fund_participation_icp_e8s` field of the output protobuf, instead of the correct `value.max_neurons_fund_participation_icp_e8s`. Any downstream consumer that deserializes this protobuf will construct a `ValidatedNeuronsFundParticipationConstraints` with a corrupted hard cap, causing the Neurons' Fund to contribute either less or more ICP than NNS governance approved during SNS swap finalization.

## Finding Description
The buggy conversion is confirmed at: [1](#0-0) 

Lines 509–511 correctly assign `value.min_direct_participation_threshold_icp_e8s` to `min_direct_participation_threshold_icp_e8s`. Lines 512–514 then assign the same `value.min_direct_participation_threshold_icp_e8s` to `max_neurons_fund_participation_icp_e8s` — the wrong source field.

The inverse conversion (`TryFrom<&NeuronsFundParticipationConstraintsPb>`) correctly reads both fields independently: [2](#0-1) 

This means the round-trip `ValidatedNeuronsFundParticipationConstraints → NeuronsFundParticipationConstraintsPb → ValidatedNeuronsFundParticipationConstraints` silently corrupts `max_neurons_fund_participation_icp_e8s` to equal `min_direct_participation_threshold_icp_e8s`. The `max_neurons_fund_participation_icp_e8s` field is the hard cap enforced on every call to `MatchedParticipationFunction::apply` during SNS swap finalization, so the corrupted value directly controls how much ICP the Neurons' Fund contributes.

## Impact Explanation
This is a **High** severity finding. The `max_neurons_fund_participation_icp_e8s` field is the hard cap applied in `MatchedParticipationFunction::apply`. When the corrupted protobuf is deserialized and used during swap finalization, the Neurons' Fund contribution is capped at `min_direct_participation_threshold_icp_e8s` instead of the governance-approved `max_neurons_fund_participation_icp_e8s`. In the typical case (`min < max`), the Neurons' Fund contributes less ICP than approved, potentially causing an SNS swap to fail to reach its minimum ICP target. In the atypical case (`min > max`), the Neurons' Fund contributes more ICP than approved, constituting unauthorized over-minting of SNS tokens relative to the NNS-approved plan. Both cases represent a concrete SNS governance/financial integrity impact with direct user and protocol harm, matching the allowed High impact: *Significant SNS security impact with concrete user or protocol harm*.

## Likelihood Explanation
Every SNS swap with `neurons_fund_participation = true` exercises this conversion path when NNS governance computes and serializes the Neurons' Fund participation constraints. No special privileges or attacker action are required — the bug is triggered automatically by the normal swap setup flow. The corruption is silent: no error is returned, no trap is triggered, and the wrong value passes all downstream validation because `min_direct_participation_threshold_icp_e8s` is a valid `u64`.

## Recommendation
Change line 513 of `rs/sns/swap/src/neurons_fund.rs` to use the correct source field:

```rust
max_neurons_fund_participation_icp_e8s: Some(
    value.max_neurons_fund_participation_icp_e8s,  // was: value.min_direct_participation_threshold_icp_e8s
),
```

Add a round-trip test that constructs a `ValidatedNeuronsFundParticipationConstraints` with distinct values for both fields, converts to `NeuronsFundParticipationConstraintsPb` via `From`, and asserts each output field matches its respective source field.

## Proof of Concept
Using the test data from `rs/nns/governance/src/governance/test_data.rs` where `min_direct_participation_threshold_icp_e8s = 36_000 * E8` and `max_neurons_fund_participation_icp_e8s = 45_000 * E8`:

1. Construct a `ValidatedNeuronsFundParticipationConstraints` with these distinct values.
2. Call `NeuronsFundParticipationConstraintsPb::from(constraints)`.
3. Observe the resulting protobuf has `max_neurons_fund_participation_icp_e8s = 36_000 * E8` (wrong; should be `45_000 * E8`).
4. Deserialize via `TryFrom<&NeuronsFundParticipationConstraintsPb>` — the reconstructed struct has `max_neurons_fund_participation_icp_e8s = 36_000 * E8`.
5. Call `MatchedParticipationFunction::apply` — the hard cap is 36,000 ICP instead of 45,000 ICP, silently reducing Neurons' Fund contribution by up to 9,000 ICP per swap.

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
