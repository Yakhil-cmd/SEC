### Title
Wrong Field Assignment Silently Corrupts `max_neurons_fund_participation_icp_e8s` During Serialization - (File: `rs/sns/swap/src/neurons_fund.rs`)

### Summary
In the `From<ValidatedNeuronsFundParticipationConstraints<F>> for NeuronsFundParticipationConstraintsPb` implementation, `max_neurons_fund_participation_icp_e8s` is populated with the value of `min_direct_participation_threshold_icp_e8s` instead of `max_neurons_fund_participation_icp_e8s`. This is a copy-paste field confusion bug that silently produces a structurally valid but semantically incorrect protobuf, analogous to the bonding curve `stepsize * numSteps != curveSupply` class of parameter-consistency bugs.

### Finding Description

In `rs/sns/swap/src/neurons_fund.rs` at lines 502–527, the `From` impl that converts a validated in-memory `ValidatedNeuronsFundParticipationConstraints<F>` back to the wire-format `NeuronsFundParticipationConstraintsPb` contains the following:

```rust
impl<F> From<ValidatedNeuronsFundParticipationConstraints<F>>
    for NeuronsFundParticipationConstraintsPb
where
    F: IdealMatchingFunction,
{
    fn from(value: ValidatedNeuronsFundParticipationConstraints<F>) -> Self {
        Self {
            min_direct_participation_threshold_icp_e8s: Some(
                value.min_direct_participation_threshold_icp_e8s,   // ✓ correct
            ),
            max_neurons_fund_participation_icp_e8s: Some(
                value.min_direct_participation_threshold_icp_e8s,   // ✗ BUG: should be
                                                                    //   value.max_neurons_fund_participation_icp_e8s
            ),
            ...
        }
    }
}
``` [1](#0-0) 

The `ValidatedNeuronsFundParticipationConstraints` struct holds both fields as distinct values: [2](#0-1) 

The two fields carry entirely different semantics:
- `min_direct_participation_threshold_icp_e8s` — the minimum direct ICP that must be raised before the Neurons' Fund participates at all.
- `max_neurons_fund_participation_icp_e8s` — the hard cap on total Neurons' Fund ICP contribution to the swap.

In every realistic SNS swap, `min_direct_participation_threshold_icp_e8s` is substantially smaller than `max_neurons_fund_participation_icp_e8s` (e.g., 36,000 ICP vs. 45,000 ICP in the test data). [3](#0-2) 

### Impact Explanation

Wherever this `From` conversion is invoked, the resulting `NeuronsFundParticipationConstraintsPb` carries a `max_neurons_fund_participation_icp_e8s` that equals the threshold value rather than the true cap. The SNS Swap canister reads this field directly from the stored `Init.neurons_fund_participation_constraints` to cap the Neurons' Fund contribution: [4](#0-3) 

With the corrupted cap, the Neurons' Fund participation is silently capped at `min_direct_participation_threshold_icp_e8s` (e.g., 36,000 ICP) instead of the intended `max_neurons_fund_participation_icp_e8s` (e.g., 45,000 ICP). Consequences:

1. **Under-participation**: The Neurons' Fund contributes less ICP than the NNS Governance intended and the SNS creator expected, potentially causing a swap to fail to reach `min_direct_participation_icp_e8s`.
2. **Silent misconfiguration**: No error is raised; the protobuf is structurally valid and passes all downstream validation because `min_direct_participation_threshold_icp_e8s` is itself a valid `u64`. The `apply()` function in `ValidatedNeuronsFundParticipationConstraints` will simply return `max_neurons_fund_participation_icp_e8s` (the corrupted, smaller value) for any direct participation above the last interval boundary. [5](#0-4) 

### Likelihood Explanation

The `From` impl is production code in the SNS Swap crate (`rs/sns/swap`). It is the only provided conversion from the validated in-memory type back to the protobuf wire type, and Rust's type system will silently select it wherever `NeuronsFundParticipationConstraintsPb::from(validated)` or `.into()` is called. Any code path that round-trips through validation and then re-serializes — including canister upgrade state migration, inter-canister forwarding, or any future use of this conversion — will produce the corrupted value. The bug passes all existing tests because the `From` impl is not exercised by a round-trip test that checks `max_neurons_fund_participation_icp_e8s` on the output.

### Recommendation

Change line 513 from:

```rust
max_neurons_fund_participation_icp_e8s: Some(
    value.min_direct_participation_threshold_icp_e8s,
),
```

to:

```rust
max_neurons_fund_participation_icp_e8s: Some(
    value.max_neurons_fund_participation_icp_e8s,
),
```

Add a round-trip test that constructs a `ValidatedNeuronsFundParticipationConstraints`, converts it to `NeuronsFundParticipationConstraintsPb` via `From`, and asserts that `max_neurons_fund_participation_icp_e8s` in the output equals the original `max_neurons_fund_participation_icp_e8s`, not `min_direct_participation_threshold_icp_e8s`.

### Proof of Concept

```
ValidatedNeuronsFundParticipationConstraints {
    min_direct_participation_threshold_icp_e8s: 36_000 * E8,   // e.g. 3_600_000_000_000
    max_neurons_fund_participation_icp_e8s:     45_000 * E8,   // e.g. 4_500_000_000_000
    coefficient_intervals: [...],
    ideal_matched_participation_function: ...,
}

// After From conversion (rs/sns/swap/src/neurons_fund.rs:507-526):
NeuronsFundParticipationConstraintsPb {
    min_direct_participation_threshold_icp_e8s: Some(3_600_000_000_000),
    max_neurons_fund_participation_icp_e8s:     Some(3_600_000_000_000),  // ← wrong, should be 4_500_000_000_000
    ...
}

// SNS Swap canister caps NF participation at 3_600_000_000_000 instead of 4_500_000_000_000
// (rs/sns/swap/src/swap.rs:583-590), silently under-funding the swap by up to 900_000_000_000 e8s (9,000 ICP).
``` [6](#0-5)

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

**File:** rs/nervous_system/neurons_fund/src/lib.rs (L294-300)
```rust
#[derive(Clone, Debug)]
pub struct ValidatedNeuronsFundParticipationConstraints<F> {
    pub min_direct_participation_threshold_icp_e8s: u64,
    pub max_neurons_fund_participation_icp_e8s: u64,
    pub coefficient_intervals: Vec<ValidatedLinearScalingCoefficient>,
    pub ideal_matched_participation_function: Box<F>,
}
```

**File:** rs/nervous_system/neurons_fund/src/lib.rs (L1002-1006)
```rust
        // Special case B: direct_participation_icp_e8s is greated than or equal to the last
        // interval's upper bound.
        if last_interval.to_direct_participation_icp_e8s <= direct_participation_icp_e8s {
            return Ok(self.max_neurons_fund_participation_icp_e8s);
        }
```

**File:** rs/nns/governance/src/governance/test_data.rs (L205-233)
```rust
    pub static ref NEURONS_FUND_PARTICIPATION_CONSTRAINTS: NeuronsFundParticipationConstraints = NeuronsFundParticipationConstraints {
        min_direct_participation_threshold_icp_e8s: Some(
            36_000 * E8,
        ),
        max_neurons_fund_participation_icp_e8s: Some(
            45_000 * E8,
        ),
        coefficient_intervals: vec![LinearScalingCoefficient {
            from_direct_participation_icp_e8s: Some(0),
            to_direct_participation_icp_e8s: Some(u64::MAX),
            slope_numerator: Some(1),
            slope_denominator: Some(1),
            intercept_icp_e8s: Some(0),
        }],
        ideal_matched_participation_function: Some(IdealMatchedParticipationFunction {
            serialized_representation: Some(
                PolynomialMatchingFunction::new(
                    u64::MAX,
                    NeuronsFundParticipationLimits {
                        max_theoretical_neurons_fund_participation_amount_icp: dec!(333_000.0),
                        contribution_threshold_icp: dec!(33_000.0),
                        one_third_participation_milestone_icp: dec!(100_000.0),
                        full_participation_milestone_icp: dec!(167_000.0),
                    },
                    false
                ).unwrap().serialize(),
            ),
        }),
    };
```

**File:** rs/sns/swap/src/swap.rs (L583-591)
```rust
                        neurons_fund_participation_icp_e8s.min(
                            // Defaulting to `u64::MAX` since we are computing minimum. Practically,
                            // this shouldn't happen, as `max_neurons_fund_participation_icp_e8s`
                            // is expected to be set here.
                            constraints
                                .max_neurons_fund_participation_icp_e8s
                                .unwrap_or(u64::MAX),
                        )
                    }
```
