### Title
Unprivileged Caller Can Reset Neuron `aging_since_timestamp_seconds` Without Owner Consent, Reducing Voting Power - (File: rs/nns/governance/src/governance.rs)

---

### Summary

Any unprivileged ingress sender can force a reset of a victim's NNS neuron age by (1) transferring a small amount of ICP to the victim's neuron subaccount on the ICP ledger, then (2) calling `manage_neuron` with `ClaimOrRefresh { by: NeuronIdOrSubaccount }` for that neuron. The `refresh_neuron` path has no caller authorization check. When the ledger balance exceeds the cached stake, `update_stake_adjust_age` is invoked unconditionally, which resets `aging_since_timestamp_seconds` to a more recent weighted-average value. This permanently reduces the neuron's age bonus and therefore its voting power, without the neuron owner's knowledge or consent.

---

### Finding Description

**Entry point — no caller check on `ClaimOrRefresh` via `NeuronIdOrSubaccount`:**

`refresh_neuron_by_id_or_subaccount` is reached from `manage_neuron` when the command is `ClaimOrRefresh { by: NeuronIdOrSubaccount }`. Neither the dispatch site nor the function itself checks that the caller is the neuron controller or a hot key. [1](#0-0) 

**`refresh_neuron` unconditionally calls `update_stake_adjust_age` when balance > cached stake:** [2](#0-1) 

**`update_stake_adjust_age` resets `aging_since_timestamp_seconds` to a weighted average:**

When new stake is added (balance > cached), the function computes a weighted-average age using `combine_aged_stakes`, where the added portion carries age = 0. This moves `aging_since_timestamp_seconds` forward in time, reducing the neuron's effective age. [3](#0-2) 

The `adjust_age` helper confirms this only affects `NotDissolving` neurons (the ones that accumulate age bonus): [4](#0-3) 

**Neuron age directly affects voting power:**

The age bonus can contribute up to 25% additional voting power for neurons aged ≥ 4 years. Resetting the age to near-zero eliminates this bonus. [5](#0-4) 

---

### Impact Explanation

An attacker who sends `X` ICP to a victim's neuron subaccount and then calls `ClaimOrRefresh { by: NeuronIdOrSubaccount }` causes the neuron's effective age to be diluted by the ratio `old_stake / (old_stake + X)`. For example, a neuron with 100 ICP staked for 4 years that receives an unsolicited 100 ICP top-up will have its age halved to 2 years, losing half its age bonus. The victim cannot prevent or undo this: the ICP is now locked in their neuron (they gain stake but lose age), and the age bonus cannot be recovered without waiting years. This constitutes a governance manipulation attack: an adversary can selectively reduce the voting power of specific neurons (e.g., neurons known to vote against the attacker's preferred proposals) without any privileged access.

---

### Likelihood Explanation

The attack requires only:
1. Knowledge of the victim's neuron ID (neuron IDs are public via `list_neurons`).
2. Ability to compute the neuron's subaccount (deterministic from neuron ID).
3. Enough ICP to transfer to the subaccount (the existing neuron balance already satisfies `neuron_minimum_stake_e8s`, so even 1 e8 triggers the age update).
4. A single `manage_neuron` ingress call — no privileged role required.

The ICP sent is not lost to the attacker in the traditional sense (it becomes part of the victim's neuron stake), but it is locked. The cost is low relative to the governance impact on high-value neurons. The attack is repeatable: the attacker can continuously drip small amounts to keep the neuron's age near zero.

---

### Recommendation

1. **Require caller authorization for `ClaimOrRefresh { by: NeuronIdOrSubaccount }`**: Only the neuron controller or a hot key should be permitted to trigger a stake refresh that modifies `aging_since_timestamp_seconds`. The `By::MemoAndController` path already implicitly scopes to the controller; the `NeuronIdOrSubaccount` path should do the same.

2. **Separate stake-update from age-update**: When a refresh is triggered by a non-owner, update `cached_neuron_stake_e8s` to reflect the true balance but do not adjust `aging_since_timestamp_seconds`. Age should only be recalculated when the owner explicitly acknowledges the new stake.

3. **Minimum added-stake threshold**: Require that the balance increase exceeds a meaningful threshold (e.g., `neuron_minimum_stake_e8s`) before triggering an age adjustment, to deter dust-based repeated attacks.

---

### Proof of Concept

```
// Attacker knows victim's neuron_id N with controller P, subaccount S, stake 100 ICP, age 4 years.

// Step 1: Transfer 100 ICP to victim's neuron subaccount via ICP ledger.
ledger.transfer({
    to: AccountIdentifier::new(GOVERNANCE_CANISTER_ID, Some(S)),
    amount: 100_ICP,
    fee: DEFAULT_FEE,
    memo: 0,
    from_subaccount: None,
});

// Step 2: Call manage_neuron as any principal (no auth check).
governance.manage_neuron(attacker_principal, ManageNeuron {
    neuron_id_or_subaccount: Some(NeuronIdOrSubaccount::NeuronId(N)),
    id: None,
    command: Some(Command::ClaimOrRefresh(ClaimOrRefresh {
        by: Some(By::NeuronIdOrSubaccount(Empty {})),
    })),
});

// Result: refresh_neuron() is called, balance (200 ICP) > cached_stake (100 ICP),
// update_stake_adjust_age(200 ICP, now) is called,
// new_age = (100 ICP * 4 years) / 200 ICP = 2 years,
// aging_since_timestamp_seconds is reset to now - 2_years.
// Victim's age bonus is halved without their consent.
```

The root cause is at: [1](#0-0) [6](#0-5) [7](#0-6)

### Citations

**File:** rs/nns/governance/src/governance.rs (L5875-5896)
```rust
    async fn refresh_neuron_by_id_or_subaccount(
        &mut self,
        id: NeuronIdOrSubaccount,
        claim_or_refresh: &ClaimOrRefresh,
    ) -> Result<NeuronId, GovernanceError> {
        let (nid, subaccount) = match id {
            NeuronIdOrSubaccount::NeuronId(neuron_id) => {
                let neuron_subaccount =
                    self.with_neuron(&neuron_id, |neuron| neuron.subaccount())?;
                (neuron_id, neuron_subaccount)
            }
            NeuronIdOrSubaccount::Subaccount(subaccount_bytes) => {
                let subaccount = Self::bytes_to_subaccount(&subaccount_bytes)?;
                let neuron_id = self
                    .neuron_store
                    .get_neuron_id_for_subaccount(subaccount)
                    .ok_or_else(|| Self::no_neuron_for_subaccount_error(&subaccount.0))?;
                (neuron_id, subaccount)
            }
        };
        self.refresh_neuron(nid, subaccount, claim_or_refresh).await
    }
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

**File:** rs/nns/governance/src/neuron/types.rs (L146-152)
```rust
    /// refresh the voting power of a neuron: set following, or vote directly.
    /// When this becomes > 6 months ago, the amount of voting power that this
    /// neuron can exercise decreases linearly down to 0 over the course of 1
    /// month. After that, following is cleared, except for ManageNeuron
    /// proposals.
    voting_power_refreshed_timestamp_seconds: u64,
    /// This field is used to store the index of the most recent ballot in the
```

**File:** rs/nns/governance/src/neuron/types.rs (L999-1039)
```rust
    pub fn update_stake_adjust_age(&mut self, updated_stake_e8s: u64, now: u64) {
        // If the updated stake is less than the original stake, preserve the
        // age and distribute it over the new amount. This should not happen
        // in practice, so this code exists merely as a defensive fallback.
        //
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
            // A consequence of the math above is that the 'new_stake_e8s' is
            // always the same as the 'updated_stake_e8s'. We use
            // 'combine_aged_stakes' here to make sure the age is
            // appropriately pro-rated to accommodate the new stake.
            assert!(new_stake_e8s == updated_stake_e8s);
            self.cached_neuron_stake_e8s = new_stake_e8s;

            let new_aging_since_timestamp_seconds = now.saturating_sub(new_age_seconds);
            let new_disolved_dissolve_state_and_age = self
                .dissolve_state_and_age()
                .adjust_age(new_aging_since_timestamp_seconds);
            self.set_dissolve_state_and_age(new_disolved_dissolve_state_and_age);
        }
```

**File:** rs/nns/governance/src/neuron/dissolve_state_and_age.rs (L253-268)
```rust
    // Adjusts the neuron age while respecting the invariant that dissolving/dissolved should not
    // have age.
    pub fn adjust_age(self, new_aging_since_timestamp_seconds: u64) -> Self {
        match self {
            // The is the only meaningful case where we adjust the age.
            Self::NotDissolving {
                dissolve_delay_seconds,
                aging_since_timestamp_seconds: _,
            } => Self::NotDissolving {
                dissolve_delay_seconds,
                aging_since_timestamp_seconds: new_aging_since_timestamp_seconds,
            },
            // This is a no-op.
            Self::DissolvingOrDissolved { .. } => self,
        }
    }
```
