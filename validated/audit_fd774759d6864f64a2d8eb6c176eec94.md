Based on my research, I can now synthesize the findings. The vulnerability class is a **stale checkpoint/timestamp bug** — when a position's principal is adjusted, the reference timestamp is not updated, causing the new amount to receive an inflated time-based bonus as if it had been present from the beginning.

In the IC codebase, the NNS governance neuron system has an analogous pattern: when `stake_maturity_of_neuron` is called, `staked_maturity_e8s_equivalent` is increased but `aging_since_timestamp_seconds` is **not** adjusted, while the voting power formula applies the full age bonus to the combined stake (including the newly staked maturity).

---

### Title
Stale `aging_since_timestamp_seconds` When Staking Maturity Inflates Neuron Voting Power - (`rs/nns/governance/src/governance.rs`, `rs/nns/governance/src/neuron/mod.rs`)

### Summary
When a neuron controller calls `stake_maturity_of_neuron`, the `staked_maturity_e8s_equivalent` field is increased but `aging_since_timestamp_seconds` is never adjusted. Because the voting power formula applies the full age bonus to the entire stake (including newly staked maturity), the newly staked maturity is treated as if it had been locked since the neuron's birth, granting an inflated age bonus on tokens that were only recently committed.

### Finding Description

The NNS governance `stake_maturity_of_neuron` function directly mutates `staked_maturity_e8s_equivalent` without calling `update_stake_adjust_age`:

```rust
// rs/nns/governance/src/governance.rs ~line 2821
let response = self.with_neuron_mut(id, |neuron| {
    neuron.maturity_e8s_equivalent = neuron
        .maturity_e8s_equivalent
        .saturating_sub(maturity_to_stake);
    neuron.staked_maturity_e8s_equivalent = Some(
        neuron.staked_maturity_e8s_equivalent
            .unwrap_or(0)
            .saturating_add(maturity_to_stake),
    );
    // aging_since_timestamp_seconds is NEVER updated here
    ...
})
``` [1](#0-0) 

The `neuron_stake_e8s` helper (used by `stake_e8s()`) includes `staked_maturity_e8s_equivalent` in the total:

```rust
// rs/nns/governance/src/neuron/mod.rs
fn neuron_stake_e8s(...) -> u64 {
    cached_neuron_stake_e8s
        .saturating_sub(neuron_fees_e8s)
        .saturating_add(staked_maturity_e8s_equivalent.unwrap_or(0))
}
``` [2](#0-1) 

The voting power formula then applies the age bonus to the full combined stake:

```rust
// rs/nns/governance/src/neuron/types.rs
let stake_e8s = self.stake_e8s();  // includes staked_maturity_e8s_equivalent
let boost = dissolve_delay_bonus_multiplier(...)
    * age_bonus_multiplier(self.age_seconds(now_seconds)); // uses aging_since_timestamp_seconds
let mut potential_voting_power = Decimal::from(stake_e8s) * boost;
``` [3](#0-2) 

By contrast, when ICP is added to a neuron's ledger account and `update_stake_adjust_age` is called, the age is correctly pro-rated using `combine_aged_stakes`, treating the new ICP as having age 0:

```rust
// rs/nns/governance/src/neuron/types.rs ~line 1021
let (new_stake_e8s, new_age_seconds) = combine_aged_stakes(
    self.cached_neuron_stake_e8s,
    self.age_seconds(now),
    updated_stake_e8s.saturating_sub(self.cached_neuron_stake_e8s),
    0,  // new stake has age 0
);
``` [4](#0-3) 

The same pattern exists in SNS governance's `stake_maturity_of_neuron`, which also increases `staked_maturity_e8s_equivalent` without calling `update_stake`: [5](#0-4) 

And the SNS `voting_power_stake_e8s()` similarly includes staked maturity in the age-boosted total: [6](#0-5) 

### Impact Explanation

A neuron holder can accumulate maturity over time (through voting rewards) and then stake it via `stake_maturity_of_neuron`. The newly staked maturity receives the full age bonus of the neuron (up to 1.25×) as if it had been locked since `aging_since_timestamp_seconds`, when in reality it was only committed at the time of the call. This inflates the neuron's voting power beyond what is warranted, distorting NNS/SNS governance outcomes. The maximum overstatement is 25% of the staked maturity's contribution to voting power (the maximum age bonus).

### Likelihood Explanation

Any neuron controller can call `stake_maturity_of_neuron` permissionlessly via `manage_neuron`. Neurons that have been non-dissolving for years and have accumulated significant maturity are common on mainnet. The call requires no special privilege beyond controlling the neuron. The effect is proportional to the neuron's age and the amount of maturity staked.

### Recommendation

When `stake_maturity_of_neuron` is called, treat the newly staked maturity as having age 0 and pro-rate the neuron's `aging_since_timestamp_seconds` using `combine_aged_stakes` (or `update_stake_adjust_age`), exactly as is done when ICP is added to the neuron's ledger account. This ensures the age bonus is only applied to stake that has actually been locked for the corresponding duration.

### Proof of Concept

1. At time T=0, create a neuron with 100 ICP stake. `aging_since_timestamp_seconds = 0`.
2. Wait 4 years (the maximum age for the 1.25× bonus). The neuron accumulates, say, 10 ICP worth of maturity through voting rewards.
3. Call `stake_maturity_of_neuron` with 100% percentage. `staked_maturity_e8s_equivalent` becomes 10 ICP. `aging_since_timestamp_seconds` remains 0.
4. Voting power is now `(100 + 10) * 1.25 * dissolve_bonus = 137.5 * dissolve_bonus`.
5. Correct voting power should be `100 * 1.25 * dissolve_bonus + 10 * 1.0 * dissolve_bonus = 135 * dissolve_bonus` (the 10 ICP of staked maturity has age 0).
6. The neuron receives `2.5 * dissolve_bonus` extra voting power units — a ~1.85% overstatement on the total, sourced entirely from the stale age timestamp on the staked maturity. [7](#0-6) [8](#0-7)

### Citations

**File:** rs/nns/governance/src/governance.rs (L2821-2840)
```rust
        let response = self
            .with_neuron_mut(id, |neuron| {
                neuron.maturity_e8s_equivalent = neuron
                    .maturity_e8s_equivalent
                    .saturating_sub(maturity_to_stake);

                neuron.staked_maturity_e8s_equivalent = Some(
                    neuron
                        .staked_maturity_e8s_equivalent
                        .unwrap_or(0)
                        .saturating_add(maturity_to_stake),
                );
                let staked_maturity_e8s = neuron.staked_maturity_e8s_equivalent.unwrap_or(0);

                StakeMaturityResponse {
                    maturity_e8s: neuron.maturity_e8s_equivalent,
                    staked_maturity_e8s,
                }
            })
            .expect("Expected the neuron to exist");
```

**File:** rs/nns/governance/src/neuron/mod.rs (L10-18)
```rust
fn neuron_stake_e8s(
    cached_neuron_stake_e8s: u64,
    neuron_fees_e8s: u64,
    staked_maturity_e8s_equivalent: Option<u64>,
) -> u64 {
    cached_neuron_stake_e8s
        .saturating_sub(neuron_fees_e8s)
        .saturating_add(staked_maturity_e8s_equivalent.unwrap_or(0))
}
```

**File:** rs/nns/governance/src/neuron/mod.rs (L22-46)
```rust
pub fn combine_aged_stakes(
    x_stake_e8s: u64,
    x_age_seconds: u64,
    y_stake_e8s: u64,
    y_age_seconds: u64,
) -> (u64, u64) {
    if x_stake_e8s == 0 && y_stake_e8s == 0 {
        (0, 0)
    } else {
        let total_age_seconds: u128 = ((x_stake_e8s as u128)
            .saturating_mul(x_age_seconds as u128)
            .saturating_add((y_stake_e8s as u128).saturating_mul(y_age_seconds as u128)))
            / ((x_stake_e8s as u128).saturating_add(y_stake_e8s as u128));

        // Note that age is adjusted in proportion to the stake, but due to the
        // discrete nature of u64 numbers, some resolution is lost due to the
        // division above. Only if x_age * x_stake is a multiple of y_stake does
        // the age remain constant after this operation. However, in the end, the
        // most that can be lost due to rounding from the actual age, is always
        // less than 1 second, so this is not a problem.
        (
            x_stake_e8s.saturating_add(y_stake_e8s),
            total_age_seconds as u64,
        )
    }
```

**File:** rs/nns/governance/src/neuron/types.rs (L376-379)
```rust
        let stake_e8s = self.stake_e8s();
        let boost = dissolve_delay_bonus_multiplier(self.dissolve_delay_seconds(now_seconds))
            * age_bonus_multiplier(self.age_seconds(now_seconds));
        let mut potential_voting_power = Decimal::from(stake_e8s) * boost;
```

**File:** rs/nns/governance/src/neuron/types.rs (L1021-1026)
```rust
            let (new_stake_e8s, new_age_seconds) = combine_aged_stakes(
                self.cached_neuron_stake_e8s,
                self.age_seconds(now),
                updated_stake_e8s.saturating_sub(self.cached_neuron_stake_e8s),
                0,
            );
```

**File:** rs/sns/governance/src/governance.rs (L1572-1591)
```rust
        // Adjust the maturity of the neuron
        let neuron = self
            .get_neuron_result_mut(nid)
            .expect("Expected the neuron to exist");

        neuron.maturity_e8s_equivalent = neuron
            .maturity_e8s_equivalent
            .saturating_sub(maturity_to_stake);

        neuron.staked_maturity_e8s_equivalent = Some(
            neuron
                .staked_maturity_e8s_equivalent
                .unwrap_or(0)
                .saturating_add(maturity_to_stake),
        );

        Ok(StakeMaturityResponse {
            maturity_e8s: neuron.maturity_e8s_equivalent,
            staked_maturity_e8s: neuron.staked_maturity_e8s_equivalent.unwrap_or(0),
        })
```

**File:** rs/sns/governance/src/neuron.rs (L641-645)
```rust
    fn voting_power_stake_e8s(&self) -> u64 {
        self.cached_neuron_stake_e8s
            .saturating_sub(self.neuron_fees_e8s)
            .saturating_add(self.staked_maturity_e8s_equivalent.unwrap_or(0))
    }
```

**File:** rs/nns/governance/src/neuron/voting_power.rs (L23-31)
```rust
pub(crate) fn age_bonus_multiplier(age_seconds: u64) -> Decimal {
    let age_seconds = Decimal::from(age_seconds.clamp(0, MAX_NEURON_AGE_FOR_AGE_BONUS));

    // t is (clamped) age in units of max age, so its value is from 0.0 to 1.0
    let t = age_seconds / Decimal::from(MAX_NEURON_AGE_FOR_AGE_BONUS);

    // 0.25 * t + 1
    t / Decimal::from(4) + Decimal::from(1)
}
```
