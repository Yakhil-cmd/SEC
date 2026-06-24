Audit Report

## Title
Unprivileged Caller Can Dilute Neuron Age Bonus via Unauthenticated `ClaimOrRefresh` - (File: rs/nns/governance/src/governance.rs)

## Summary
Any unprivileged principal can reduce a victim NNS neuron's age bonus (up to 25% of voting power) by transferring ICP to the neuron's subaccount and then calling `manage_neuron` with `ClaimOrRefresh { by: NeuronIdOrSubaccount }`. The `refresh_neuron_by_id_or_subaccount` function performs no caller authorization check, and `refresh_neuron` unconditionally invokes `update_stake_adjust_age` when the ledger balance exceeds the cached stake, permanently resetting `aging_since_timestamp_seconds` to a weighted-average value without the neuron owner's consent.

## Finding Description
**No caller authorization in `refresh_neuron_by_id_or_subaccount`:**
The function at `rs/nns/governance/src/governance.rs` L5875–5896 resolves the neuron from a public `NeuronIdOrSubaccount` and immediately delegates to `refresh_neuron`. Neither this function nor its caller checks that the ingress sender is the neuron controller or a hot key.

**Unconditional age mutation in `refresh_neuron`:**
At L5950–5951, when `balance > cached_neuron_stake_e8s` (`Ordering::Less`), `neuron.update_stake_adjust_age(balance.get_e8s(), now)` is called with no authorization gate. The only guard (L5924–5934) checks that the balance meets `neuron_minimum_stake_e8s`, which is already satisfied by any existing neuron.

**`update_stake_adjust_age` computes a diluted age:**
At `rs/nns/governance/src/neuron/types.rs` L1021–1038, `combine_aged_stakes` is called with the added portion carrying `age = 0`. The resulting `new_aging_since_timestamp_seconds = now - new_age_seconds` is then passed to `adjust_age`, which for `NotDissolving` neurons (L255–263 of `dissolve_state_and_age.rs`) overwrites `aging_since_timestamp_seconds` with the diluted value. The neuron's effective age is reduced proportionally to `old_stake / (old_stake + added_stake)`.

**Exploit flow:**
1. Attacker looks up victim's `neuron_id` (public via `list_neurons`) and computes the deterministic subaccount.
2. Attacker transfers `X` ICP to the neuron's subaccount on the ICP ledger.
3. Attacker calls `manage_neuron` as any principal with `ClaimOrRefresh { by: By::NeuronIdOrSubaccount }` targeting the victim's neuron.
4. `refresh_neuron` reads the new balance, finds `balance > cached_stake`, and calls `update_stake_adjust_age`, permanently reducing `aging_since_timestamp_seconds`.

## Impact Explanation
The age bonus contributes up to 25% additional voting power for neurons staked ≥ 4 years. An attacker who matches the victim's stake (e.g., 100 ICP against a 100 ICP neuron) halves the effective age, eliminating half the age bonus. The attack is repeatable: continuous small transfers keep the age near zero indefinitely. The victim cannot recover the lost age without waiting years. This constitutes targeted, permanent governance voting-power reduction against specific neurons — matching the **High** bounty impact: *Unauthorized access to neurons/governance assets where exploitation requires meaningful per-target work or other constraints* ($2,000–$10,000).

## Likelihood Explanation
Prerequisites are minimal: neuron IDs are public, subaccounts are deterministically computable, and the only cost is locking ICP in the victim's neuron (not destroyed, but illiquid). No privileged role is required. The attack is repeatable with dust amounts to continuously suppress age. High-value neurons (large stake, long age) are the most attractive targets and the most impactful to attack.

## Recommendation
1. **Add caller authorization to `refresh_neuron_by_id_or_subaccount`**: Verify that the caller is the neuron controller or a registered hot key before proceeding, consistent with how other neuron-mutating commands are gated.
2. **Decouple stake update from age update for unauthenticated callers**: Update `cached_neuron_stake_e8s` to reflect the true ledger balance, but only adjust `aging_since_timestamp_seconds` when the neuron owner explicitly triggers the refresh.
3. **Minimum added-stake threshold**: Require the balance increase to exceed `neuron_minimum_stake_e8s` before triggering an age adjustment, to deter dust-based repeated attacks.

## Proof of Concept
```rust
// 1. Attacker transfers 100 ICP to victim neuron subaccount (neuron has 100 ICP, age 4 years).
ledger.transfer(AccountIdentifier::new(GOVERNANCE_ID, Some(victim_subaccount)), 100_ICP, ...);

// 2. Attacker calls manage_neuron as any principal — no auth check.
governance.manage_neuron(attacker_principal, ManageNeuron {
    neuron_id_or_subaccount: Some(NeuronIdOrSubaccount::NeuronId(victim_neuron_id)),
    command: Some(Command::ClaimOrRefresh(ClaimOrRefresh {
        by: Some(By::NeuronIdOrSubaccount(Empty {})),
    })),
});
// Result: balance (200 ICP) > cached_stake (100 ICP) → update_stake_adjust_age called
// new_age = (100 ICP * 4 years) / 200 ICP = 2 years
// aging_since_timestamp_seconds reset to now - 2_years; age bonus halved permanently.
```
A deterministic integration test using PocketIC can reproduce this by: (a) creating a neuron, (b) advancing time 4 years, (c) having a second identity transfer ICP to the neuron subaccount and call `ClaimOrRefresh`, (d) asserting that `age_seconds` is approximately halved. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

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
