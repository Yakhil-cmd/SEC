### Title
Neurons' Fund `intercept_icp_e8s` Computation Has Silent u64 Overflow via `saturating_mul` - (File: `rs/nns/governance/src/neurons_fund.rs`)

### Summary
The `compute_linear_scaling_coefficients` function multiplies `capped.neurons.len() as u64` by `max_participant_icp_e8s` (also `u64`) using `saturating_mul`. When the true product exceeds `u64::MAX`, Rust's `saturating_mul` silently clamps to `u64::MAX` rather than returning the correct value or an error. The corrupted `intercept_icp_e8s` is then stored in the `LinearScalingCoefficient` and consumed by `MatchedParticipationFunction::apply`, which determines how much ICP the Neurons' Fund contributes to an SNS swap.

### Finding Description
Inside `NeuronsFundParticipation::compute_linear_scaling_coefficients`:

```rust
let intercept_icp_e8s = Some(
    (capped.neurons.len() as u64)
        .saturating_mul(self.swap_participation_limits.max_participant_icp_e8s),
);
```

`capped.neurons.len()` is the count of Neurons' Fund neurons whose proportional maturity exceeds `max_participant_icp_e8s`. Both operands are `u64`. Their product represents the total ICP e8s that capped neurons contribute as a fixed offset (the intercept). If `n_capped × max_participant_icp_e8s > u64::MAX ≈ 1.84 × 10¹⁹`, `saturating_mul` silently returns `u64::MAX` instead of the correct value or an error.

The corrupted value flows directly into `MatchedParticipationFunction::apply`:

```rust
let intercept_icp = rescale_to_icp(*intercept_icp_e8s)?;   // divides by 1e8
let effective_icp = hard_cap_icp.min(intercept_icp.saturating_add(
    (slope_numerator / slope_denominator) * ideal_icp,
));
return rescale_to_icp_e8s(effective_icp);
```

A saturated `intercept_icp_e8s = u64::MAX` converts to `≈ 1.84 × 10¹¹ ICP`, which is far above any realistic `hard_cap_icp`, so `effective_icp` collapses to `hard_cap_icp` for every call in that interval — the Neurons' Fund participates at its absolute ceiling regardless of actual direct participation.

The analogous correct pattern already used elsewhere in the same codebase (e.g., `combine_aged_stakes` in `rs/nns/governance/src/neuron/mod.rs`) is to widen both operands to `u128` before multiplying:

```rust
let total_age_seconds: u128 = ((x_stake_e8s as u128)
    .saturating_mul(x_age_seconds as u128) …
```

### Impact Explanation
A corrupted `intercept_icp_e8s` forces `effective_icp` to equal `hard_cap_icp` for the affected interval, causing the Neurons' Fund to over-participate in an SNS swap — committing more ICP than the matching function warrants. This misallocates Neurons' Fund treasury funds and distorts the SNS token distribution, potentially harming NNS neuron holders whose maturity backs the fund.

### Likelihood Explanation
Overflow requires `n_capped × max_participant_icp_e8s > 1.84 × 10¹⁹`. With a realistic `max_participant_icp_e8s` of `10,000 ICP = 10¹² e8s`, overflow needs `n_capped > 18,400,000` capped neurons — far beyond the current Neurons' Fund population. Likelihood is therefore very low, analogous to the Oracle.sol finding being downgraded to medium.

### Recommendation
Widen both operands to `u128` before multiplying, then clamp back to `u64` with an explicit error if the result exceeds `u64::MAX`:

```rust
let intercept_icp_e8s_u128 = (capped.neurons.len() as u128)
    .checked_mul(self.swap_participation_limits.max_participant_icp_e8s as u128)
    .ok_or_else(|| "intercept_icp_e8s overflow".to_string())?;
let intercept_icp_e8s = u64::try_from(intercept_icp_e8s_u128)
    .map_err(|_| "intercept_icp_e8s exceeds u64::MAX".to_string())?;
```

### Proof of Concept
Suppose an SNS swap sets `max_participant_icp_e8s = 100_000 * E8` (100,000 ICP = 10¹³ e8s) and the Neurons' Fund has 2,000,000 neurons all capped at that level:

```
n_capped × max_participant_icp_e8s
= 2_000_000 × 10_000_000_000_000
= 2 × 10¹⁹  >  u64::MAX ≈ 1.84 × 10¹⁹
```

`saturating_mul` returns `u64::MAX`. `rescale_to_icp(u64::MAX) ≈ 184,467,440,737 ICP`. Any realistic `hard_cap_icp` (e.g., 95,000 ICP) is smaller, so `effective_icp = hard_cap_icp` for every `direct_participation_icp_e8s` in that interval, forcing maximum Neurons' Fund participation regardless of actual swap demand. [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** rs/nns/governance/src/neurons_fund.rs (L1037-1044)
```rust
            max_neurons_fund_swap_participation_icp_e8s,
            ideal_matched_participation_function.apply_and_rescale_to_icp_e8s(
                swap_participation_limits.max_direct_participation_icp_e8s,
            )?,
        );
        let ideal_matched_participation_function_value_icp_e8s =
            ideal_matched_participation_function
                .apply_and_rescale_to_icp_e8s(direct_participation_icp_e8s)?;
```

**File:** rs/nns/governance/src/neurons_fund.rs (L1287-1290)
```rust
                    let intercept_icp_e8s = Some(
                        (capped.neurons.len() as u64)
                            .saturating_mul(self.swap_participation_limits.max_participant_icp_e8s),
                    );
```

**File:** rs/nns/governance/src/neuron/mod.rs (L31-34)
```rust
        let total_age_seconds: u128 = ((x_stake_e8s as u128)
            .saturating_mul(x_age_seconds as u128)
            .saturating_add((y_stake_e8s as u128).saturating_mul(y_age_seconds as u128)))
            / ((x_stake_e8s as u128).saturating_add(y_stake_e8s as u128));
```
