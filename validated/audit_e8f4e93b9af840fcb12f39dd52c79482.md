### Title
Wrong Field Used in `From<ValidatedNeuronsFundParticipationConstraints>` Conversion Causes Incorrect `max_neurons_fund_participation_icp_e8s` — (File: `rs/sns/swap/src/neurons_fund.rs`)

---

### Summary

In `rs/sns/swap/src/neurons_fund.rs`, the `From<ValidatedNeuronsFundParticipationConstraints<F>> for NeuronsFundParticipationConstraintsPb` implementation contains a copy-paste error: `max_neurons_fund_participation_icp_e8s` is populated with `value.min_direct_participation_threshold_icp_e8s` instead of `value.max_neurons_fund_participation_icp_e8s`. This is the direct IC analog of the external report's class — a function returning the wrong scalar (a count/wrong field) where a different value is required, corrupting a downstream cap/weight calculation.

---

### Finding Description

The conversion implementation at `rs/sns/swap/src/neurons_fund.rs` lines 502–526 reads:

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

The two fields being confused are semantically opposite:

| Field | Meaning |
|---|---|
| `min_direct_participation_threshold_icp_e8s` | Minimum direct participation before NF participates at all (small) |
| `max_neurons_fund_participation_icp_e8s` | Hard cap on total NF participation (large) |

The `ValidatedNeuronsFundParticipationConstraints` struct holds both as distinct `u64` fields: [2](#0-1) 

When the `apply` function on `ValidatedNeuronsFundParticipationConstraints` is called, it uses `max_neurons_fund_participation_icp_e8s` as a hard cap:

```rust
let hard_cap_icp = rescale_to_icp(self.max_neurons_fund_participation_icp_e8s)?;
...
let effective_icp = hard_cap_icp.min(intercept_icp.saturating_add(
    (slope_numerator / slope_denominator) * ideal_icp,
));
``` [3](#0-2) 

If the protobuf produced by the buggy `From` impl is re-validated and used (e.g., stored in canister state and later deserialized), `max_neurons_fund_participation_icp_e8s` will equal `min_direct_participation_threshold_icp_e8s` — a value orders of magnitude smaller — causing `hard_cap_icp` to be far too low and the effective NF participation to be severely underestimated.

---

### Impact Explanation

The Neurons' Fund participation amount directly determines how much ICP is minted and sent to the SNS governance treasury upon swap commitment (`settle_neurons_fund_participation`). If `max_neurons_fund_participation_icp_e8s` is silently replaced with the minimum threshold value, the hard cap applied in `MatchedParticipationFunction::apply` will truncate the effective participation to a tiny fraction of the intended amount. This constitutes a **ledger conservation / chain-fusion mint/burn accounting bug**: the SNS treasury receives far less ICP than the Neurons' Fund neurons' maturity warrants, and the refund logic returns excess maturity to neurons based on the wrong effective amount. [4](#0-3) 

---

### Likelihood Explanation

The `From` implementation is production code (not gated by `#[cfg(test)]`) in the SNS Swap canister crate. Any code path that converts a `ValidatedNeuronsFundParticipationConstraints` back to protobuf — for storage, inter-canister messaging, or re-validation — will silently produce a corrupted `max_neurons_fund_participation_icp_e8s`. The SNS Swap canister is invoked by unprivileged direct participants whose `refresh_buyer_tokens` calls trigger `update_total_participation_amounts`, which internally validates and applies the constraints. If the constraints are ever round-tripped through this `From` impl, the bug is triggered without any privileged access. [5](#0-4) 

---

### Recommendation

Fix the copy-paste error in the `From` implementation:

```rust
max_neurons_fund_participation_icp_e8s: Some(
    value.max_neurons_fund_participation_icp_e8s, // ← correct field
),
```

Add a unit test that round-trips a `ValidatedNeuronsFundParticipationConstraints` through `From` and asserts that `max_neurons_fund_participation_icp_e8s` in the resulting protobuf equals the original `max_neurons_fund_participation_icp_e8s`, not `min_direct_participation_threshold_icp_e8s`.

---

### Proof of Concept

The bug is self-evident from the source:

```
rs/sns/swap/src/neurons_fund.rs, lines 507–514:

    min_direct_participation_threshold_icp_e8s: Some(
        value.min_direct_participation_threshold_icp_e8s,   // correct
    ),
    max_neurons_fund_participation_icp_e8s: Some(
        value.min_direct_participation_threshold_icp_e8s,   // ← wrong field; should be
    ),                                                       //   value.max_neurons_fund_participation_icp_e8s
``` [6](#0-5) 

A concrete scenario: if `min_direct_participation_threshold_icp_e8s = 36_000 * E8` and `max_neurons_fund_participation_icp_e8s = 45_000 * E8` (as in the test data), the serialized protobuf will carry `max_neurons_fund_participation_icp_e8s = 36_000 * E8`. Upon re-validation and application, the hard cap is `36_000 ICP` instead of `45_000 ICP`, silently reducing every NF participation computation that falls in the `[36_000, 45_000]` ICP range to exactly `36_000 ICP`. [7](#0-6)

### Citations

**File:** rs/sns/swap/src/neurons_fund.rs (L502-526)
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

**File:** rs/nervous_system/neurons_fund/src/lib.rs (L1025-1043)
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
```

**File:** rs/nns/governance/src/neurons_fund.rs (L929-932)
```rust
    /// Returns the total Neurons' Fund participation amount.
    pub fn total_amount_icp_e8s(&self) -> u64 {
        self.allocated_neurons_fund_participation_icp_e8s
    }
```

**File:** rs/sns/swap/src/swap.rs (L540-603)
```rust
    fn update_total_participation_amounts(&mut self) {
        let direct_participation_icp_e8s = self
            .buyers
            .values()
            .map(|x| x.amount_icp_e8s())
            .fold(0_u64, |sum, v| sum.saturating_add(v));
        self.direct_participation_icp_e8s = Some(direct_participation_icp_e8s);

        let (neurons_fund_participation, neurons_fund_participation_constraints) =
            if let Some(init) = &self.init {
                (
                    &init.neurons_fund_participation,
                    &init.neurons_fund_participation_constraints,
                )
            } else {
                return;
            };
        match (
            neurons_fund_participation,
            neurons_fund_participation_constraints,
        ) {
            (Some(true), Some(constraints)) => {
                // Matched funding scheme
                let participation: PolynomialNeuronsFundParticipation = match constraints.try_into()
                {
                    Ok(participation) => participation,
                    Err(err) => {
                        log!(
                            ERROR,
                            "Cannot validate swap.init.neurons_fund_participation_constraints: {}",
                            err.to_string(),
                        );
                        return;
                    }
                };
                let neurons_fund_participation_icp_e8s = match MatchedParticipationFunction::apply(
                    &participation,
                    direct_participation_icp_e8s,
                ) {
                    Ok(neurons_fund_participation_icp_e8s) => {
                        // Capping mitigates a potentially confusing situation in which the Swap's
                        // best `neurons_fund_participation_icp_e8s` estimate for whatever reason
                        // exceeds the amount allocated by the Neurons' Fund before the swap started.
                        neurons_fund_participation_icp_e8s.min(
                            // Defaulting to `u64::MAX` since we are computing minimum. Practically,
                            // this shouldn't happen, as `max_neurons_fund_participation_icp_e8s`
                            // is expected to be set here.
                            constraints
                                .max_neurons_fund_participation_icp_e8s
                                .unwrap_or(u64::MAX),
                        )
                    }
                    Err(err) => {
                        log!(
                            ERROR,
                            "Cannot compute neurons_fund_participation_icp_e8s for \
                        direct_participation_icp_e8s={}: {}",
                            direct_participation_icp_e8s,
                            err.to_string(),
                        );
                        return;
                    }
                };
                self.neurons_fund_participation_icp_e8s = Some(neurons_fund_participation_icp_e8s);
```

**File:** rs/nns/governance/src/governance/test_data.rs (L205-218)
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
```
