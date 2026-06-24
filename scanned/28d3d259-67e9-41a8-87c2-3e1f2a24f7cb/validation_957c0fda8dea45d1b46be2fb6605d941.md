### Title
Incorrect Age Accounting When Refreshing Neuron Stake with Accumulated Fees Inflates Voting Power - (File: rs/nns/governance/src/neuron/types.rs)

### Summary
`update_stake_adjust_age` in NNS Governance (and `update_stake` in SNS Governance) uses `cached_neuron_stake_e8s` — the gross stake including accumulated rejection fees — as the "old stake" in the age-dilution calculation when a neuron's stake is refreshed. However, voting power is computed using the *effective* stake (`cached_neuron_stake_e8s - neuron_fees_e8s`). This mismatch causes the neuron's age to be diluted less than it should be, granting inflated age-bonus voting power to any neuron that has accumulated fees and then tops up its stake.

### Finding Description
When a neuron holder tops up their neuron's ledger account and calls `ClaimOrRefresh`, `update_stake_adjust_age` is invoked with the new ledger balance. The age-dilution formula is:

```
new_age = (cached_neuron_stake_e8s × old_age) / updated_stake_e8s
``` [1](#0-0) 

The function uses `self.cached_neuron_stake_e8s` (gross, includes `neuron_fees_e8s`) as the weight for the existing stake. However, the voting power calculation uses `stake_e8s()` = `cached_neuron_stake_e8s - neuron_fees_e8s`: [2](#0-1) 

The age bonus in voting power is then applied to the *net* stake but with an age derived from the *gross* stake. The correct formula should weight the old age by the effective stake:

```
correct_new_age = (effective_stake × old_age) / (effective_stake + new_icp_added)
               = ((cached - fees) × old_age) / (updated_stake - fees)
```

The developers acknowledge this discrepancy with an open TODO: [3](#0-2) 

The same issue exists in SNS Governance's `update_stake`: [4](#0-3) 

The entry path is `manage_neuron` → `ClaimOrRefresh` → `refresh_neuron` → `update_stake_adjust_age`: [5](#0-4) 

### Impact Explanation
A neuron with `cached_neuron_stake_e8s = C`, `neuron_fees_e8s = F` (where F is large), and age = A that tops up with T ICP receives:

- **Actual new age** = `(A × C) / (C + T)`
- **Correct new age** = `(A × (C−F)) / (C−F+T)`

**Concrete example**: C=100 ICP, F=90 ICP, T=100 ICP, A=max_age (4 years):
- Actual new age = 2 years → age bonus multiplier = 1.125
- Correct new age = 0.36 years → age bonus multiplier = 1.023
- Effective stake after top-up = 110 ICP
- Actual voting power ≈ 123.75 ICP-equivalent
- Correct voting power ≈ 112.5 ICP-equivalent
- **~10% voting power inflation**

This inflated voting power affects NNS governance proposals (subnet upgrades, protocol changes, treasury decisions) and SNS governance proposals. The age bonus is up to 25% (NNS) or configurable (SNS). [6](#0-5) 

### Likelihood Explanation
Any unprivileged neuron holder can trigger this by:
1. Making proposals that get rejected (accumulating `neuron_fees_e8s`; NNS rejection fee is 1 ICP per proposal)
2. Transferring ICP to the neuron's ledger subaccount
3. Calling `manage_neuron` with `ClaimOrRefresh`

The economic cost (burning ICP via rejected proposals) limits practical exploitation, but the path is fully permissionless and reachable via standard ingress. Neurons that accumulate fees organically (e.g., through governance participation where proposals are rejected) are affected without any deliberate manipulation.

### Recommendation
Replace `self.cached_neuron_stake_e8s` with the effective stake (`self.cached_neuron_stake_e8s.saturating_sub(self.neuron_fees_e8s)`) as the weight for the existing stake in `update_stake_adjust_age` and `update_stake`. The age-dilution should reflect the economically meaningful stake, consistent with how voting power is computed:

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

This resolves the open TODO(NNS1-954) and aligns the age accounting with the voting power calculation.

### Proof of Concept
1. Create NNS neuron with 100 ICP, non-dissolving, dissolve delay ≥ 6 months, wait 4 years to accumulate max age.
2. Make 90 proposals that get rejected → `neuron_fees_e8s = 90 ICP`.
3. Transfer 100 ICP to the neuron's ledger subaccount.
4. Call `manage_neuron` with `ClaimOrRefresh { by: NeuronIdOrSubaccount(...) }`.
5. Observe `age_seconds` ≈ 2 years (from `(4yr × 100) / 200`).
6. Correct value should be ≈ 0.36 years (from `(4yr × 10) / 110`).
7. Voting power = `stake_e8s() × age_bonus_multiplier(2yr)` = `110 × 1.125` ≈ 123.75 ICP-equivalent, vs correct ≈ 112.5 ICP-equivalent — a ~10% inflation exploitable in any NNS or SNS governance vote. [7](#0-6)

### Citations

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

**File:** rs/nns/governance/src/neuron/types.rs (L1003-1006)
```rust
        //
        // TODO(NNS1-954) Consider whether update_stake_adjust_age (and other
        // similar methods) should use a neurons effective stake rather than
        // the cached stake.
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

**File:** rs/sns/governance/src/neuron.rs (L649-657)
```rust
    pub fn update_stake(&mut self, new_stake_e8s: u64, now: u64) {
        // If this neuron has an age and its stake is being increased, adjust the
        // neuron's age
        if self.aging_since_timestamp_seconds < now && self.cached_neuron_stake_e8s <= new_stake_e8s
        {
            let old_stake = self.cached_neuron_stake_e8s as u128;
            let old_age = now.saturating_sub(self.aging_since_timestamp_seconds) as u128;
            let new_age = (old_age * old_stake) / (new_stake_e8s as u128);

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
