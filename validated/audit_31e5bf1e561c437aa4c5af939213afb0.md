### Title
Wrong Field Used in `NeuronsFundParticipationConstraints` Serialization Causes Neurons' Fund Hard Cap to Be Set to Minimum Threshold Instead of Maximum — (`File: rs/sns/swap/src/neurons_fund.rs`)

### Summary
In `rs/sns/swap/src/neurons_fund.rs`, the `From<ValidatedNeuronsFundParticipationConstraints<F>> for NeuronsFundParticipationConstraintsPb` implementation assigns `value.min_direct_participation_threshold_icp_e8s` to the `max_neurons_fund_participation_icp_e8s` field instead of `value.max_neurons_fund_participation_icp_e8s`. This is a direct analog to the external report's "wrong variable used in a calculation" class of bug: a semantically incorrect field substitution causes the Neurons' Fund participation hard cap to be serialized as the minimum threshold value, which is then consumed by the SNS swap canister to enforce a drastically lower cap than governance intended.

### Finding Description

In `rs/sns/swap/src/neurons_fund.rs` lines 502–527, the `From` conversion that serializes `ValidatedNeuronsFundParticipationConstraints` into the protobuf `NeuronsFundParticipationConstraintsPb` contains a copy-paste error:

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
                value.min_direct_participation_threshold_icp_e8s, // ← BUG: should be value.max_neurons_fund_participation_icp_e8s
            ),
            ...
        }
    }
}
``` [1](#0-0) 

The two fields have entirely different semantics:
- `min_direct_participation_threshold_icp_e8s`: the minimum amount of direct ICP participation required before the Neurons' Fund participates at all (typically a small value, e.g., tens of thousands of ICP e8s).
- `max_neurons_fund_participation_icp_e8s`: the hard cap on total Neurons' Fund ICP participation in the swap (typically orders of magnitude larger).

The serialized protobuf is sent from NNS governance to the SNS swap canister. The swap canister deserializes it and uses `max_neurons_fund_participation_icp_e8s` as the hard cap in two places within `rs/nervous_system/neurons_fund/src/lib.rs`:

1. As the return value when direct participation exceeds the last interval's upper bound: [2](#0-1) 

2. As `hard_cap_icp` that clamps the computed effective participation: [3](#0-2) 

Because the serialized `max_neurons_fund_participation_icp_e8s` is actually `min_direct_participation_threshold_icp_e8s`, the hard cap enforced by the swap canister is far below what governance intended.

### Impact Explanation

Every SNS swap that involves Neurons' Fund matched funding will have its Neurons' Fund participation capped at `min_direct_participation_threshold_icp_e8s` rather than `max_neurons_fund_participation_icp_e8s`. In practice these values differ by orders of magnitude. The result is:

- The Neurons' Fund contributes far less ICP than governance approved and intended.
- SNS swaps may fail to reach their `min_direct_participation_icp_e8s` target (since matched funding is suppressed), causing swaps to abort and SNS projects to fail to decentralize.
- ICP that the Neurons' Fund neurons were supposed to deploy into productive swaps remains idle, harming the economic design of matched funding.

This is a **ledger conservation / governance authorization bug**: the governance-approved participation amount is silently replaced by a much smaller value during serialization, with no error or warning.

### Likelihood Explanation

This code path is exercised for every SNS swap that opts into Neurons' Fund matched funding. The `From` conversion is called unconditionally during swap initialization whenever NNS governance computes and forwards participation constraints to the swap canister. No special attacker action is required — any SNS project that creates a swap proposal with Neurons' Fund participation triggers the bug. The bug is deterministic and reproducible.

### Recommendation

Fix line 513 in `rs/sns/swap/src/neurons_fund.rs` to use the correct field:

```rust
max_neurons_fund_participation_icp_e8s: Some(
    value.max_neurons_fund_participation_icp_e8s, // corrected
),
```

Add a unit test that round-trips a `ValidatedNeuronsFundParticipationConstraints` through the `From` conversion and asserts that `max_neurons_fund_participation_icp_e8s` in the output equals the input's `max_neurons_fund_participation_icp_e8s`, not its `min_direct_participation_threshold_icp_e8s`.

### Proof of Concept

Given a `ValidatedNeuronsFundParticipationConstraints` with:
- `min_direct_participation_threshold_icp_e8s` = 50_000 * E8 (50,000 ICP)
- `max_neurons_fund_participation_icp_e8s` = 1_000_000 * E8 (1,000,000 ICP)

After the `From` conversion, the resulting `NeuronsFundParticipationConstraintsPb` will have:
- `max_neurons_fund_participation_icp_e8s` = 50_000 * E8 (wrong — should be 1,000,000 * E8)

When the SNS swap canister deserializes this and calls `MatchedParticipationFunction::apply`, the `hard_cap_icp` will be 50,000 ICP instead of 1,000,000 ICP. All Neurons' Fund participation will be clamped to 50,000 ICP regardless of how much direct participation occurs, silently suppressing up to 950,000 ICP of intended matched funding. [4](#0-3) [5](#0-4)

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

**File:** rs/nervous_system/neurons_fund/src/lib.rs (L1003-1006)
```rust
        // interval's upper bound.
        if last_interval.to_direct_participation_icp_e8s <= direct_participation_icp_e8s {
            return Ok(self.max_neurons_fund_participation_icp_e8s);
        }
```

**File:** rs/nervous_system/neurons_fund/src/lib.rs (L1037-1044)
```rust
            let effective_icp = hard_cap_icp.min(intercept_icp.saturating_add(
                // `slope_denominator` cannot be zero as it has been validated.
                // See `LinearScalingCoefficientValidationError::DenominatorIsZero`.
                // `slope_numerator / slope_denominator` is between 0.0 and 1.0.
                // See `LinearScalingCoefficientValidationError::NumeratorGreaterThanDenominator`.
                (slope_numerator / slope_denominator) * ideal_icp,
            ));
            return rescale_to_icp_e8s(effective_icp);
```
