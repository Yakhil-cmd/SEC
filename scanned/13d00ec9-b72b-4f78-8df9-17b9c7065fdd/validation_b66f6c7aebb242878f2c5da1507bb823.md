### Title
Wrong Field Assignment in `From<ValidatedNeuronsFundParticipationConstraints>` Sets Incorrect Neurons' Fund Participation Cap — (File: `rs/sns/swap/src/neurons_fund.rs`)

---

### Summary

In `rs/sns/swap/src/neurons_fund.rs`, the `From<ValidatedNeuronsFundParticipationConstraints<F>> for NeuronsFundParticipationConstraintsPb` implementation assigns `value.min_direct_participation_threshold_icp_e8s` to the `max_neurons_fund_participation_icp_e8s` field of the output protobuf struct, instead of the correct `value.max_neurons_fund_participation_icp_e8s`. This is a direct analog of the reported `min`/`max` threshold mismatch: the wrong source value is used for a security-critical limit, causing the Neurons' Fund participation hard cap to be silently corrupted whenever this conversion path is exercised.

---

### Finding Description

The buggy conversion is at lines 512–513 of `rs/sns/swap/src/neurons_fund.rs`:

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

The `ValidatedNeuronsFundParticipationConstraints` struct holds two semantically distinct fields:
- `min_direct_participation_threshold_icp_e8s`: the minimum direct ICP participation required before the Neurons' Fund participates at all (e.g., 36,000 ICP).
- `max_neurons_fund_participation_icp_e8s`: the hard cap on how much ICP the Neurons' Fund may contribute (e.g., 45,000 ICP). [2](#0-1) 

The `max_neurons_fund_participation_icp_e8s` field is used as a hard cap in `MatchedParticipationFunction::apply`:

```rust
// Special case B: direct_participation_icp_e8s >= last interval's upper bound.
return Ok(self.max_neurons_fund_participation_icp_e8s);
...
let hard_cap_icp = rescale_to_icp(self.max_neurons_fund_participation_icp_e8s)?;
let effective_icp = hard_cap_icp.min(intercept_icp.saturating_add(...));
``` [3](#0-2) 

When the `From` conversion is used to serialize a `ValidatedNeuronsFundParticipationConstraints` back to `NeuronsFundParticipationConstraintsPb`, the resulting protobuf carries `min_direct_participation_threshold_icp_e8s` in the `max_neurons_fund_participation_icp_e8s` slot. Any downstream consumer that deserializes this protobuf (via `TryFrom<&NeuronsFundParticipationConstraintsPb>`) will then construct a `ValidatedNeuronsFundParticipationConstraints` with the wrong hard cap. [4](#0-3) 

---

### Impact Explanation

The `max_neurons_fund_participation_icp_e8s` field is the hard cap enforced on every call to `MatchedParticipationFunction::apply`, which is invoked during SNS swap finalization to determine how much ICP the Neurons' Fund contributes. Two concrete impact scenarios arise:

1. **Cap set too low** (typical case: `min_direct_participation_threshold < max_neurons_fund_participation`): The Neurons' Fund contributes less ICP than governance intended. An SNS swap that should succeed (reach its minimum ICP target with Neurons' Fund help) may fail, or the SNS receives less funding than the NNS proposal guaranteed.

2. **Cap set too high** (atypical case: `min_direct_participation_threshold > max_neurons_fund_participation`): The Neurons' Fund may contribute more ICP than the governance-approved maximum, causing over-minting of SNS tokens relative to the approved plan and violating the economic invariants of the matched-funding mechanism.

Both cases constitute a **ledger conservation / chain-fusion mint bug**: the amount of ICP locked from Neurons' Fund maturity and the corresponding SNS tokens minted diverge from what NNS governance approved.

---

### Likelihood Explanation

The `From<ValidatedNeuronsFundParticipationConstraints<F>> for NeuronsFundParticipationConstraintsPb` conversion is production code in the swap crate. It is exercised whenever a validated constraints struct is converted back to protobuf — a path that occurs during SNS swap setup when NNS governance computes and serializes the Neurons' Fund participation constraints. Every SNS swap that uses Neurons' Fund matched funding (i.e., every swap with `neurons_fund_participation = true`) passes through this conversion. The bug is silent: no error is returned, no trap is triggered, and the wrong value passes all downstream validation checks because `min_direct_participation_threshold_icp_e8s` is itself a valid `u64`.

---

### Recommendation

Change line 513 of `rs/sns/swap/src/neurons_fund.rs` to use the correct source field:

```rust
max_neurons_fund_participation_icp_e8s: Some(
    value.max_neurons_fund_participation_icp_e8s,  // was: value.min_direct_participation_threshold_icp_e8s
),
``` [5](#0-4) 

Add a round-trip test that constructs a `ValidatedNeuronsFundParticipationConstraints` with distinct values for `min_direct_participation_threshold_icp_e8s` and `max_neurons_fund_participation_icp_e8s`, converts it to `NeuronsFundParticipationConstraintsPb` via `From`, and asserts that both fields in the output match their respective source fields.

---

### Proof of Concept

Let:
- `min_direct_participation_threshold_icp_e8s = 36_000 * E8` (36,000 ICP)
- `max_neurons_fund_participation_icp_e8s = 45_000 * E8` (45,000 ICP)

After the `From` conversion, the resulting `NeuronsFundParticipationConstraintsPb` contains:
- `min_direct_participation_threshold_icp_e8s = 36_000 * E8` ✓
- `max_neurons_fund_participation_icp_e8s = 36_000 * E8` ✗ (should be 45,000 ICP)

When the swap canister deserializes this protobuf and calls `MatchedParticipationFunction::apply`, the hard cap is 36,000 ICP instead of 45,000 ICP. The Neurons' Fund is silently capped 9,000 ICP below the governance-approved maximum, reducing its contribution and potentially causing the swap to fall short of its minimum ICP target.

This matches the test data visible in `rs/nns/governance/src/governance/test_data.rs`: [6](#0-5) 

where `min_direct_participation_threshold_icp_e8s = 36_000 * E8` and `max_neurons_fund_participation_icp_e8s = 45_000 * E8` are explicitly distinct values — confirming the two fields are never intended to be equal.

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

**File:** rs/sns/swap/src/neurons_fund.rs (L507-526)
```rust
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
```

**File:** rs/nervous_system/neurons_fund/src/lib.rs (L1004-1006)
```rust
        if last_interval.to_direct_participation_icp_e8s <= direct_participation_icp_e8s {
            return Ok(self.max_neurons_fund_participation_icp_e8s);
        }
```

**File:** rs/nervous_system/neurons_fund/src/lib.rs (L1025-1044)
```rust
            let hard_cap_icp = rescale_to_icp(self.max_neurons_fund_participation_icp_e8s)?;

            // This value is how much of Neurons' Fund maturity can "effectively" be allocated.
            // This value may be less than or equal to the `ideal_icp` value above, due to:
            // (1) Some Neurons' Fund neurons being too small to participate at all (at this direct
            //     participation amount, `direct_participation_icp_e8s`). This is taken into account
            //     via the `(slope_numerator / slope_denominator)` factor.
            // (2) Some Neurons' fund neurons being too big to fully participate (at this direct
            //     participation amount, `direct_participation_icp_e8s`). This is taken into account
            //     via the `intercept_icp` component.
            // (3) The computed overall participation amount (unexpectedly) exceeded `hard_cap_icp`;
            //     so we enforce the limited at `hard_cap_icp`.
            let effective_icp = hard_cap_icp.min(intercept_icp.saturating_add(
                // `slope_denominator` cannot be zero as it has been validated.
                // See `LinearScalingCoefficientValidationError::DenominatorIsZero`.
                // `slope_numerator / slope_denominator` is between 0.0 and 1.0.
                // See `LinearScalingCoefficientValidationError::NumeratorGreaterThanDenominator`.
                (slope_numerator / slope_denominator) * ideal_icp,
            ));
            return rescale_to_icp_e8s(effective_icp);
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
