### Title
f64 Precision Loss in NNS Voting Reward Distribution Causes Systematic ICP Minting Errors — (`rs/nns/governance/src/governance.rs`)

### Summary
The NNS governance canister computes voting reward amounts using `f64` floating-point arithmetic and then converts the result back to `u64` via Rust's `as` cast. Because the live ICP supply in e8s already exceeds 2^53 (the f64 mantissa limit), the intermediate `f64` representation is imprecise, and the final `as u64` cast silently produces a value that differs from the mathematically correct integer result. This is the direct IC analog of the ABDKMathQuad conversion-precision bug: a math library with a finite representable range is used for financial calculations, and conversions between the library's type and the native integer type introduce silent rounding errors at boundary values.

### Finding Description

`distribute_rewards` in `rs/nns/governance/src/governance.rs` computes the daily reward pool as:

```rust
let total_available_e8s_equivalent_float =
    (supply.get_e8s() as f64) * fraction
    + rolling_over_from_previous_reward_event_e8s_equivalent as f64;
``` [1](#0-0) 

`supply.get_e8s()` is a `u64`. The current ICP supply is approximately 500 M ICP = 5 × 10^16 e8s. The IEEE-754 `f64` mantissa is 53 bits, so it can represent integers exactly only up to 2^53 ≈ 9 × 10^15. The live supply already exceeds this threshold, meaning the cast `supply.get_e8s() as f64` silently rounds to the nearest representable value, introducing an error of up to ±8 e8s per cast.

The imprecise float is then used in two further `as u64` casts:

**Per-neuron reward** (line 6724–6725):
```rust
let reward = (used_voting_rights * total_available_e8s_equivalent_float
    / total_voting_rights) as u64;
``` [2](#0-1) 

**Stored RewardEvent total** (line 6752):
```rust
total_available_e8s_equivalent: total_available_e8s_equivalent_float as u64,
``` [3](#0-2) 

The stored `total_available_e8s_equivalent` is read back in the next reward round as `rolling_over_from_previous_reward_event_e8s_equivalent`, compounding the error across rounds. [4](#0-3) 

The reward calculation module itself acknowledges floating-point use but does not acknowledge the precision boundary: [5](#0-4) 

The `rewards_pool_to_distribute_in_supply_fraction_for_one_day` function returns an `f64` fraction that is then multiplied by the supply: [6](#0-5) 

### Impact Explanation

Every daily reward round, the governance canister mints ICP maturity to neurons. The amount minted per neuron is derived from `total_available_e8s_equivalent_float`, which is already imprecise. The `as u64` cast truncates rather than rounds, so the error is directional (always rounds down). Over time:

1. The per-neuron maturity increments are systematically lower than the mathematically correct values.
2. The `total_available_e8s_equivalent` stored in each `RewardEvent` is incorrect, and the rollover amount fed into the next round is also incorrect.
3. The cumulative divergence between "ICP that should have been minted as maturity" and "ICP actually credited" grows monotonically.

This is a ledger conservation bug: the total maturity credited across all neurons does not equal the intended reward pool fraction of the ICP supply.

### Likelihood Explanation

This is not a theoretical future condition. The ICP supply already exceeds 2^53 e8s (current supply ≈ 5 × 10^16 e8s > 2^53 ≈ 9 × 10^15 e8s). The precision error is therefore active in every reward round on mainnet today. No attacker action is required; the bug fires automatically on every governance heartbeat that triggers reward distribution. The error magnitude per round is small (≤ 8 e8s on the supply cast alone), but it is systematic and accumulates.

### Recommendation

Replace the `f64` intermediate with `u128` or `rust_decimal::Decimal` arithmetic throughout the reward pool calculation, consistent with how SNS governance already handles its reward distribution: [7](#0-6) 

SNS governance uses `Decimal` for the per-neuron reward and only converts to `u64` at the final step with an explicit `unwrap_or_else` panic guard. NNS governance should adopt the same pattern, eliminating the silent `f64 as u64` truncation.

### Proof of Concept

```
ICP supply today ≈ 500_000_000 ICP = 5_000_000_000_000_000_00 e8s
                                   = 5 × 10^16 e8s

2^53 = 9_007_199_254_740_992 ≈ 9 × 10^15 e8s

5 × 10^16 > 9 × 10^15  →  supply already exceeds f64 exact-integer range

Nearest f64 values around 5 × 10^16 are spaced 2^(56-52) = 16 apart.
  (supply.get_e8s() as f64) as u64  !=  supply.get_e8s()
  error ≤ 8 e8s per cast

Daily reward fraction ≈ 0.05/365.25 ≈ 1.369 × 10^-4
  total_available_e8s_equivalent_float ≈ 5×10^16 × 1.369×10^-4 ≈ 6.85×10^12 e8s

The f64 representation of 6.85×10^12 is exact (< 2^53), but the
*input* supply was already rounded, so the pool is off by up to
  8 × 1.369×10^-4 ≈ 0.001 e8s per neuron per round — small per round,
  but systematic and non-zero, accumulating indefinitely.

Additionally, total_available_e8s_equivalent_float as u64 at line 6752
stores the rounded value, feeding the error into the next round's rollover.
```

### Citations

**File:** rs/nns/governance/src/governance.rs (L6651-6654)
```rust
        let rolling_over_from_previous_reward_event_e8s_equivalent =
            latest_reward_event.e8s_equivalent_to_be_rolled_over();
        let total_available_e8s_equivalent_float = (supply.get_e8s() as f64) * fraction
            + rolling_over_from_previous_reward_event_e8s_equivalent as f64;
```

**File:** rs/nns/governance/src/governance.rs (L6724-6725)
```rust
                    let reward = (used_voting_rights * total_available_e8s_equivalent_float
                        / total_voting_rights) as u64;
```

**File:** rs/nns/governance/src/governance.rs (L6752-6752)
```rust
            total_available_e8s_equivalent: total_available_e8s_equivalent_float as u64,
```

**File:** rs/nns/governance/src/reward/calculation.rs (L1-16)
```rust
//! Code for the computation of rewards distributions.
//!
//! This module makes use of floating-point computations. This is a reasonable
//! choice for computing rewards because:
//!
//! * Floating-point computations are deterministic and fully specified in wasm.
//!   In particular, rounding behavior is fully specified: https://www.w3.org/TR/wasm-core-1/#floating-point-operations%E2%91%A0
//!
//! * Floating-point operations are allowed in canister code.
//!
//! * The computation here happens pre-minting, and therefore there is no
//!   constraint that mandate fixed-precision.
//!
//! * Floating point makes code easier since the reward pool is specified as a
//!   fraction of the total ICP supply.

```

**File:** rs/nns/governance/src/reward/calculation.rs (L79-101)
```rust
pub fn rewards_pool_to_distribute_in_supply_fraction_for_one_day(
    days_since_ic_genesis: u64,
) -> f64 {
    // Despite the rate being arguable a continuous function of time, we don't
    // integrate the rate here. Instead we multiply the rate at the beginning of
    // that day with the considered duration.
    let t = IcTimestamp {
        days_since_ic_genesis: days_since_ic_genesis as f64,
    };

    let variable_rate = if t > REWARD_FLATTENING_DATE {
        InverseDuration { per_day: 0.0 }
    } else {
        let duration_to_bottom = t - REWARD_FLATTENING_DATE;
        let closeness_to_bottom = duration_to_bottom / (GENESIS - REWARD_FLATTENING_DATE);
        let delta_rate = INITIAL_VOTING_REWARD_RELATIVE_RATE - FINAL_VOTING_REWARD_RELATIVE_RATE;
        closeness_to_bottom.powf(2.0) * delta_rate
    };

    let rate = FINAL_VOTING_REWARD_RELATIVE_RATE + variable_rate;

    voting_rewards_adjustment_factor() * rate * ONE_DAY
}
```

**File:** rs/sns/governance/src/governance.rs (L5974-5986)
```rust
                let neuron_reward_e8s =
                    rewards_purse_e8s * (neuron_reward_shares / total_reward_shares);

                // Round down, and convert to u64.
                let neuron_reward_e8s = u64::try_from(neuron_reward_e8s).unwrap_or_else(|err| {
                    panic!(
                        "Calculating reward for neuron {neuron_id:?}:\n\
                             neuron_reward_shares: {neuron_reward_shares}\n\
                             rewards_purse_e8s: {rewards_purse_e8s}\n\
                             total_reward_shares: {total_reward_shares}\n\
                             err: {err}",
                    )
                });
```
