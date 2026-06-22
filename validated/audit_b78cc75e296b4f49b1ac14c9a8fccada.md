### Title
Wrong Field Assigned to `max_neurons_fund_participation_icp_e8s` in `From` Conversion - (File: rs/sns/swap/src/neurons_fund.rs)

### Summary
In the `From<ValidatedNeuronsFundParticipationConstraints<F>> for NeuronsFundParticipationConstraintsPb` implementation, `max_neurons_fund_participation_icp_e8s` is incorrectly populated with `value.min_direct_participation_threshold_icp_e8s` instead of `value.max_neurons_fund_participation_icp_e8s`. This is a direct analog to the Joyn M-09 finding: a wrong constant/field is silently used in place of the correct one, causing the serialized constraints sent to or stored by the Swap canister to carry an incorrect cap on Neurons' Fund participation.

### Finding Description

In `rs/sns/swap/src/neurons_fund.rs`, the `From` conversion that serializes a validated `ValidatedNeuronsFundParticipationConstraints` back into the protobuf `NeuronsFundParticipationConstraintsPb` contains a copy-paste error:

```rust
impl<F> From<ValidatedNeuronsFundParticipationConstraints<F>>
    for NeuronsFundParticipationConstraintsPb
{
    fn from(value: ValidatedNeuronsFundParticipationConstraints<F>) -> Self {
        Self {
            min_direct_participation_threshold_icp_e8s: Some(
                value.min_direct_participation_threshold_icp_e8s,   // correct
            ),
            max_neurons_fund_participation_icp_e8s: Some(
                value.min_direct_participation_threshold_icp_e8s,   // BUG: should be value.max_neurons_fund_participation_icp_e8s
            ),
            ...
        }
    }
}
``` [1](#0-0) 

The two fields have entirely different semantics:
- `min_direct_participation_threshold_icp_e8s`: the minimum direct ICP participation below which the Neurons' Fund will not participate at all.
- `max_neurons_fund_participation_icp_e8s`: the absolute cap on how much ICP the Neurons' Fund may contribute to the swap.

The proto definition confirms these are distinct fields: [2](#0-1) 

### Impact Explanation

Wherever the Swap canister round-trips a `ValidatedNeuronsFundParticipationConstraints` through this `From` conversion (e.g., when re-serializing for storage, inter-canister response, or passing to downstream logic), the resulting `NeuronsFundParticipationConstraintsPb` will carry `max_neurons_fund_participation_icp_e8s = min_direct_participation_threshold_icp_e8s`.

Concretely:

1. **Under-participation**: If `min_direct_participation_threshold_icp_e8s` < actual `max_neurons_fund_participation_icp_e8s`, the Neurons' Fund participation is capped far below its intended maximum. Swaps that should succeed with Neurons' Fund matching may fail to reach `min_participants` or `min_direct_participation_icp_e8s`, causing the swap to abort and all participants to be refunded — locking the SNS launch.

2. **Over-participation**: If `min_direct_participation_threshold_icp_e8s` > actual `max_neurons_fund_participation_icp_e8s`, the Neurons' Fund is allowed to contribute more than the governance-approved maximum, violating the conservation invariant enforced by NNS Governance.

The validation in `rs/sns/init/src/lib.rs` checks `max_neurons_fund_participation_icp_e8s` against `max_direct_participation_icp_e8s`: [3](#0-2) 

But this check operates on the *incoming* protobuf from NNS Governance, not on the re-serialized output of this buggy `From` conversion. Once the Swap canister re-serializes via this path, the corrupted value bypasses that validation.

### Likelihood Explanation

Every SNS swap that uses Neurons' Fund matched funding will exercise this code path. The `From` conversion is the only serialization path from `ValidatedNeuronsFundParticipationConstraints` to `NeuronsFundParticipationConstraintsPb` in the Swap canister. No privileged access is required — any user triggering an SNS swap with Neurons' Fund participation enabled will cause this path to execute. The bug is deterministic and reproducible on every affected swap.

### Recommendation

Fix the copy-paste error by using the correct source field:

```rust
max_neurons_fund_participation_icp_e8s: Some(
    value.max_neurons_fund_participation_icp_e8s,  // was: value.min_direct_participation_threshold_icp_e8s
),
```

Add a unit test that constructs a `ValidatedNeuronsFundParticipationConstraints` with distinct values for `min_direct_participation_threshold_icp_e8s` and `max_neurons_fund_participation_icp_e8s`, round-trips it through `From`, and asserts both fields in the output are correct.

### Proof of Concept

Given:
- `min_direct_participation_threshold_icp_e8s = 50 * E8` (50 ICP)
- `max_neurons_fund_participation_icp_e8s = 200_000 * E8` (200,000 ICP)

After the `From` conversion:
- `output.min_direct_participation_threshold_icp_e8s = 50 * E8` ✓
- `output.max_neurons_fund_participation_icp_e8s = 50 * E8` ✗ (should be `200_000 * E8`)

The Swap canister now believes the Neurons' Fund cap is 50 ICP instead of 200,000 ICP. Any swap requiring more than 50 ICP of Neurons' Fund participation will be silently under-matched, potentially causing the swap to abort even though sufficient Neurons' Fund maturity exists. [4](#0-3)

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

**File:** rs/sns/swap/proto/ic_sns_swap/pb/v1/swap.proto (L436-455)
```text
message NeuronsFundParticipationConstraints {
  // The Neurons' Fund will not participate in this swap unless the direct
  // contributions reach this threshold (in ICP e8s).
  optional uint64 min_direct_participation_threshold_icp_e8s = 1;

  // Maximum amount (in ICP e8s) of contributions from the Neurons' Fund to this swap.
  optional uint64 max_neurons_fund_participation_icp_e8s = 2;

  // List of intervals in which the given linear coefficients apply for scaling the
  // ideal Neurons' Fund participation amount (down) to the effective Neurons' Fund
  // participation amount.
  repeated LinearScalingCoefficient coefficient_intervals = 3;

  // The function used in the implementation of Matched Funding for mapping amounts of direct
  // participation to "ideal" Neurons' Fund participation amounts. The value needs to be adjusted
  // to a potentially smaller value due to SNS-specific participation constraints and
  // the configuration of the Neurons' Fund at the time of the CreateServiceNervousSystem proposal
  // execution.
  optional IdealMatchedParticipationFunction ideal_matched_participation_function = 4;
}
```

**File:** rs/sns/init/src/lib.rs (L1819-1845)
```rust
        if 0 < max_neurons_fund_participation_icp_e8s
            && max_neurons_fund_participation_icp_e8s < min_participant_icp_e8s
        {
            let max_neurons_fund_participation_icp_e8s =
                NonZeroU64::new(max_neurons_fund_participation_icp_e8s).unwrap();
            return Result::from(NeuronsFundParticipationConstraintsValidationError::MaxNeuronsFundParticipationValidationError(
                MaxNeuronsFundParticipationValidationError::BelowSingleParticipationLimit {
                    max_neurons_fund_participation_icp_e8s,
                    min_participant_icp_e8s,
                }
            ));
        }
        // Not more than 50% of total contributions should come from the Neurons' Fund.
        let max_direct_participation_icp_e8s =
            self.max_direct_participation_icp_e8s.ok_or_else(|| {
                NeuronsFundParticipationConstraintsValidationError::RelatedFieldUnspecified(
                    "max_direct_participation_icp_e8s".to_string(),
                )
                .to_string()
            })?;
        if max_neurons_fund_participation_icp_e8s > max_direct_participation_icp_e8s {
            return Result::from(NeuronsFundParticipationConstraintsValidationError::MaxNeuronsFundParticipationValidationError(
                MaxNeuronsFundParticipationValidationError::AboveSwapMaxDirectIcp {
                    max_neurons_fund_participation_icp_e8s,
                    max_direct_participation_icp_e8s,
                }
            ));
```
