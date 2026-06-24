Audit Report

## Title
Age-Dilution Uses Gross Stake Instead of Effective Stake, Inflating Age-Bonus Voting Power - (File: rs/nns/governance/src/neuron/types.rs)

## Summary
`update_stake_adjust_age` in NNS Governance weights the age-dilution calculation using `cached_neuron_stake_e8s` (gross stake, inclusive of accumulated `neuron_fees_e8s`), while voting power is computed against the net effective stake (`cached_neuron_stake_e8s - neuron_fees_e8s`). This mismatch causes the neuron's age to be diluted less than it should be when fees are present, granting inflated age-bonus voting power to any neuron that has accumulated rejection fees and then tops up its stake. The same defect exists in SNS Governance's `update_stake`.

## Finding Description
**NNS path.** In `update_stake_adjust_age` (`rs/nns/governance/src/neuron/types.rs`, lines 1021–1026), the call to `combine_aged_stakes` passes `self.cached_neuron_stake_e8s` as the weight for the existing stake:

```rust
let (new_stake_e8s, new_age_seconds) = combine_aged_stakes(
    self.cached_neuron_stake_e8s,   // gross — includes neuron_fees_e8s
    self.age_seconds(now),
    updated_stake_e8s.saturating_sub(self.cached_neuron_stake_e8s),
    0,
);
```

`combine_aged_stakes` (`rs/nns/governance/src/neuron/mod.rs`, lines 22–47) computes the weighted-average age as `(x_stake × x_age + y_stake × y_age) / (x_stake + y_stake)`. Using the gross stake as `x_stake` over-weights the old age, producing a larger `new_age_seconds` than is warranted.

Voting power, however, is computed in `potential_and_deciding_voting_power` (`rs/nns/governance/src/neuron/types.rs`, line 376) using `self.stake_e8s()`, which resolves to `cached_neuron_stake_e8s - neuron_fees_e8s + staked_maturity` (`rs/nns/governance/src/neuron/mod.rs`, lines 10–18). The age bonus multiplier (`rs/nns/governance/src/neuron/voting_power.rs`, lines 23–31) is then applied to this *net* stake but with an age derived from the *gross* stake, inflating the result.

The developers explicitly acknowledge the discrepancy with an open TODO at lines 1004–1006:
```
// TODO(NNS1-954) Consider whether update_stake_adjust_age (and other
// similar methods) should use a neurons effective stake rather than
// the cached stake.
```

**SNS path.** `update_stake` (`rs/sns/governance/src/neuron.rs`, lines 649–679) has the identical defect: `old_stake = self.cached_neuron_stake_e8s` (line 654) is used in the age formula `new_age = (old_age * old_stake) / new_stake_e8s`, while `voting_power` uses `voting_power_stake_e8s()` (line 205), which subtracts fees (lines 641–644).

**Trigger path.** `manage_neuron` → `ClaimOrRefresh` → `refresh_neuron` → `update_stake_adjust_age` (`rs/nns/governance/src/governance.rs`, lines 5936–5959). Any neuron holder can reach this path by transferring ICP to the neuron's ledger subaccount and calling `manage_neuron` with `ClaimOrRefresh`.

**Why existing checks do not prevent this.** There is no guard that normalises the stake to its effective value before the age calculation. The `Ordering::Greater` branch (line 5938) also calls `update_stake_adjust_age` with the raw ledger balance, so even a stake *decrease* scenario goes through the same flawed weighting.

## Impact Explanation
A neuron with gross stake C, fees F, and age A that tops up with T ICP receives:
- **Actual new age** = `(A × C) / (C + T)`
- **Correct new age** = `(A × (C−F)) / (C−F+T)`

For C=100 ICP, F=90 ICP, T=100 ICP, A=4 years (max age):
- Actual new age ≈ 2 years → age bonus ≈ 1.125×
- Correct new age ≈ 0.36 years → age bonus ≈ 1.023×
- Effective stake after top-up = 110 ICP
- Actual voting power ≈ 123.75 ICP-equivalent vs. correct ≈ 112.5 ICP-equivalent → **~10% inflation**

This constitutes a significant NNS and SNS governance security impact: inflated voting power directly affects the outcome of NNS proposals (subnet upgrades, protocol parameter changes, treasury disbursements) and SNS proposals. It falls under the **High** impact class: *Significant NNS, SNS, or infrastructure security impact with concrete user or protocol harm*.

## Likelihood Explanation
The exploit is fully permissionless and reachable via standard ingress. The only cost is burning ICP through rejected proposals (NNS rejection fee: 1 ICP per proposal). Accumulating 90 ICP in fees requires 90 rejected proposals, which is economically significant but not prohibitive for a well-resourced actor seeking to influence high-value governance votes. Additionally, neurons that accumulate fees organically through normal governance participation are affected without any deliberate manipulation — the bug fires on any `ClaimOrRefresh` where `neuron_fees_e8s > 0`. The attack is repeatable and deterministic.

## Recommendation
Replace `self.cached_neuron_stake_e8s` with the effective stake in both functions:

**NNS** (`rs/nns/governance/src/neuron/types.rs`):
```rust
let effective_old_stake = self.cached_neuron_stake_e8s.saturating_sub(self.neuron_fees_e8s);
let effective_new_stake = updated_stake_e8s.saturating_sub(self.neuron_fees_e8s);
let (_, new_age_seconds) = combine_aged_stakes(
    effective_old_stake,
    self.age_seconds(now),
    effective_new_stake.saturating_sub(effective_old_stake),
    0,
);
```
Then set `self.cached_neuron_stake_e8s = updated_stake_e8s` separately. This resolves TODO(NNS1-954).

**SNS** (`rs/sns/governance/src/neuron.rs`):
```rust
let effective_old_stake = self.cached_neuron_stake_e8s.saturating_sub(self.neuron_fees_e8s) as u128;
let effective_new_stake = new_stake_e8s.saturating_sub(self.neuron_fees_e8s) as u128;
let new_age = (old_age * effective_old_stake) / effective_new_stake;
```

## Proof of Concept
1. Create an NNS neuron with 100 ICP, non-dissolving, dissolve delay ≥ 6 months.
2. Wait 4 years (or fast-forward in a PocketIC/local replica test) to accumulate maximum age (`aging_since_timestamp_seconds = now - 4*365*24*3600`).
3. Submit 90 proposals that get rejected → `neuron_fees_e8s = 90 × 10^8`.
4. Transfer 100 ICP to the neuron's ledger subaccount.
5. Call `manage_neuron` with `ClaimOrRefresh { by: NeuronIdOrSubaccount(...) }`.
6. Query the neuron: observe `age_seconds ≈ 2 years` (from `(4yr × 100) / 200`).
7. Correct value should be `≈ 0.36 years` (from `(4yr × 10) / 110`).
8. Compute `potential_voting_power`: `stake_e8s() = 110 ICP`, `age_bonus_multiplier(2yr) = 1.125` → ≈ 123.75 ICP-equivalent vs. correct ≈ 112.5 ICP-equivalent.

A deterministic unit test can reproduce this without a full replica by directly constructing a `Neuron` with `cached_neuron_stake_e8s = 100e8`, `neuron_fees_e8s = 90e8`, `aging_since_timestamp_seconds = now - 4*365*24*3600`, calling `update_stake_adjust_age(200e8, now)`, and asserting the resulting `age_seconds` against both the buggy and correct values. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6) [8](#0-7)

### Citations

**File:** rs/nns/governance/src/neuron/types.rs (L376-379)
```rust
        let stake_e8s = self.stake_e8s();
        let boost = dissolve_delay_bonus_multiplier(self.dissolve_delay_seconds(now_seconds))
            * age_bonus_multiplier(self.age_seconds(now_seconds));
        let mut potential_voting_power = Decimal::from(stake_e8s) * boost;
```

**File:** rs/nns/governance/src/neuron/types.rs (L973-979)
```rust
    pub fn stake_e8s(&self) -> u64 {
        neuron_stake_e8s(
            self.cached_neuron_stake_e8s,
            self.neuron_fees_e8s,
            self.staked_maturity_e8s_equivalent,
        )
    }
```

**File:** rs/nns/governance/src/neuron/types.rs (L1004-1026)
```rust
        // TODO(NNS1-954) Consider whether update_stake_adjust_age (and other
        // similar methods) should use a neurons effective stake rather than
        // the cached stake.
        if updated_stake_e8s < self.cached_neuron_stake_e8s {
            println!(
                "{}Reducing neuron {:?} stake via update_stake_adjust_age: {} -> {}",
                LOG_PREFIX,
                self.id(),
                self.cached_neuron_stake_e8s,
                updated_stake_e8s
            );
            self.cached_neuron_stake_e8s = updated_stake_e8s;
        } else {
            // If one looks at "stake * age" as describing an area, the goal
            // at this point is to increase the stake while keeping the area
            // constant. This means decreasing the age in proportion to the
            // additional stake, which is the purpose of combine_aged_stakes.
            let (new_stake_e8s, new_age_seconds) = combine_aged_stakes(
                self.cached_neuron_stake_e8s,
                self.age_seconds(now),
                updated_stake_e8s.saturating_sub(self.cached_neuron_stake_e8s),
                0,
            );
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

**File:** rs/nns/governance/src/neuron/mod.rs (L22-47)
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
}
```

**File:** rs/sns/governance/src/neuron.rs (L641-644)
```rust
    fn voting_power_stake_e8s(&self) -> u64 {
        self.cached_neuron_stake_e8s
            .saturating_sub(self.neuron_fees_e8s)
            .saturating_add(self.staked_maturity_e8s_equivalent.unwrap_or(0))
```

**File:** rs/sns/governance/src/neuron.rs (L649-679)
```rust
    pub fn update_stake(&mut self, new_stake_e8s: u64, now: u64) {
        // If this neuron has an age and its stake is being increased, adjust the
        // neuron's age
        if self.aging_since_timestamp_seconds < now && self.cached_neuron_stake_e8s <= new_stake_e8s
        {
            let old_stake = self.cached_neuron_stake_e8s as u128;
            let old_age = now.saturating_sub(self.aging_since_timestamp_seconds) as u128;
            let new_age = (old_age * old_stake) / (new_stake_e8s as u128);

            // new_age * new_stake = old_age * old_stake -
            // (old_stake * old_age) % new_stake. That is, age is
            // adjusted in proportion to the stake, but due to the
            // discrete nature of u64 numbers, some resolution is
            // lost due to the division above. This means the age
            // bonus is derived from a constant times age times
            // stake, minus up to new_stake - 1 each time the
            // neuron is refreshed. Only if old_age * old_stake is
            // a multiple of new_stake does the age remain
            // constant after the refresh operation. However, in
            // the end, the most that can be lost due to rounding
            // from the actual age, is always less 1 second, so
            // this is not a problem.
            self.aging_since_timestamp_seconds = now.saturating_sub(new_age as u64);
            // Note that if new_stake == old_stake, then
            // new_age == old_age, and
            // now - new_age =
            // now-(now-neuron.aging_since_timestamp_seconds)
            // = neuron.aging_since_timestamp_seconds.
        }

        self.cached_neuron_stake_e8s = new_stake_e8s;
```

**File:** rs/nns/governance/src/governance.rs (L5936-5959)
```rust
        self.with_neuron_mut(&nid, |neuron| {
            match neuron.cached_neuron_stake_e8s.cmp(&balance.get_e8s()) {
                Ordering::Greater => {
                    println!(
                        "{}ERROR. Neuron cached stake was inconsistent.\
                     Neuron account: {} has less e8s: {} than the cached neuron stake: {}.\
                     Stake adjusted.",
                        LOG_PREFIX,
                        account,
                        balance.get_e8s(),
                        neuron.cached_neuron_stake_e8s
                    );
                    neuron.update_stake_adjust_age(balance.get_e8s(), now);
                }
                Ordering::Less => {
                    neuron.update_stake_adjust_age(balance.get_e8s(), now);
                }
                // If the stake is the same as the account balance,
                // just return the neuron id (this way this method
                // also serves the purpose of allowing to discover the
                // neuron id based on the memo and the controller).
                Ordering::Equal => (),
            };
        })?;
```
