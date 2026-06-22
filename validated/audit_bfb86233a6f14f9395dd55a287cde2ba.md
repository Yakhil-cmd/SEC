### Title
Third-Party Stake Refresh Dilutes Neuron Age Bonus Without Owner Consent - (File: `rs/nns/governance/src/governance.rs`)

### Summary
Any unprivileged principal can send ICP to a victim's neuron staking subaccount and then invoke `ClaimOrRefresh` to trigger `refresh_neuron`, which calls `update_stake_adjust_age`. This dilutes the victim neuron's accumulated age bonus by treating the newly added stake as having age zero, reducing the victim's voting power and voting rewards without their consent. This is the IC analog of the griefing attack described in the report (Issue 1: "a user can deposit a small amount of new tokens for a different user, resetting their timer").

### Finding Description

The NNS governance canister exposes `ClaimOrRefresh` as a permissionless operation. The code in `claim_or_refresh_neuron_by_memo_and_controller` accepts an explicit `controller` field and imposes no caller-identity check before routing to `refresh_neuron`: [1](#0-0) 

`refresh_neuron` reads the ledger balance and, when the balance exceeds the cached stake, unconditionally calls `update_stake_adjust_age`: [2](#0-1) 

`update_stake_adjust_age` computes the new age as a weighted average of the old stake's age and the new stake's age (which is always 0): [3](#0-2) 

The `combine_aged_stakes` call passes `0` as the age of the newly added tokens, so any addition of stake permanently dilutes the neuron's accumulated age. The codebase itself documents this permissionless behavior: [4](#0-3) 

The age bonus contributes up to 25% additional voting power in NNS (and is configurable in SNS). Voting power is computed as: [5](#0-4) 

The SNS governance path has the same structure via `update_stake`: [6](#0-5) 

### Impact Explanation

An attacker who knows a victim's neuron subaccount (deterministic from `controller` principal + `memo` nonce, both often public) can:

1. Transfer ICP to the victim's neuron staking subaccount via the ledger.
2. Call `manage_neuron` with `ClaimOrRefresh { by: MemoAndController { memo, controller: victim } }`.
3. `refresh_neuron` reads the new balance and calls `update_stake_adjust_age`, diluting the victim's age bonus.

The age bonus is diluted in proportion to the ratio of added stake to total stake. To halve the age bonus the attacker must add stake equal to the existing stake. Even partial dilution reduces the victim's `deciding_voting_power` and voting rewards. In a close NNS governance vote, targeted dilution of a large neuron's age bonus could shift the outcome. The effect is permanent until the neuron re-accumulates age. [7](#0-6) 

### Likelihood Explanation

- The neuron subaccount is deterministic (`compute_neuron_staking_subaccount(controller, memo)`); for neurons created with memo=0 (common) and a known controller principal (public on the dashboard), the subaccount is trivially computable.
- The attack requires spending ICP proportional to the desired dilution, making large-scale dilution economically costly but targeted dilution of specific neurons feasible.
- No privileged access, governance majority, or threshold corruption is required — only a standard ledger transfer and a `manage_neuron` ingress call.

### Recommendation

Restrict `ClaimOrRefresh` (stake refresh path) so that only the neuron controller or an authorized hot key can trigger a refresh that results in `update_stake_adjust_age` being called. Alternatively, when the caller is not the neuron owner, allow the balance to be credited to the neuron's stake without adjusting the age (i.e., treat third-party top-ups as having the same age as the existing stake, or require explicit owner consent before age is recalculated).

### Proof of Concept

1. Victim neuron: controller = `victim_principal`, memo = `0`, stake = 1000 ICP, age = 4 years (near maximum age bonus).
2. Attacker computes subaccount: `compute_neuron_staking_subaccount(victim_principal, 0)`.
3. Attacker transfers 1000 ICP to that subaccount via the ICP ledger.
4. Attacker calls NNS governance `manage_neuron` with:
   ```
   ClaimOrRefresh { by: MemoAndController { memo: 0, controller: victim_principal } }
   ```
5. `refresh_neuron` reads balance = 2000 ICP, calls `update_stake_adjust_age(2000 ICP, now)`.
6. New age = `(4 years × 1000 ICP) / 2000 ICP` = 2 years — the age bonus is halved.
7. Victim's voting power drops by approximately 12.5% (half of the 25% max age bonus), and their voting rewards decrease proportionally. [8](#0-7)

### Citations

**File:** rs/nns/governance/src/governance.rs (L5852-5871)
```rust
    async fn claim_or_refresh_neuron_by_memo_and_controller(
        &mut self,
        caller: &PrincipalId,
        memo_and_controller: MemoAndController,
        claim_or_refresh: &ClaimOrRefresh,
    ) -> Result<NeuronId, GovernanceError> {
        let controller = memo_and_controller.controller.unwrap_or(*caller);
        let memo = memo_and_controller.memo;
        let subaccount = ledger::compute_neuron_staking_subaccount(controller, memo);
        match self.neuron_store.get_neuron_id_for_subaccount(subaccount) {
            Some(neuron_id) => {
                self.refresh_neuron(neuron_id, subaccount, claim_or_refresh)
                    .await
            }
            None => {
                self.claim_neuron(subaccount, controller, claim_or_refresh)
                    .await
            }
        }
    }
```

**File:** rs/nns/governance/src/governance.rs (L5898-5962)
```rust
    /// Refreshes the stake of a given neuron by checking it's account.
    #[cfg_attr(feature = "tla", tla_update_method(REFRESH_NEURON_DESC.clone(), tla_snapshotter!()))]
    async fn refresh_neuron(
        &mut self,
        nid: NeuronId,
        subaccount: Subaccount,
        claim_or_refresh: &ClaimOrRefresh,
    ) -> Result<NeuronId, GovernanceError> {
        let account = neuron_subaccount(subaccount);
        // We need to lock the neuron to make sure it doesn't undergo
        // concurrent changes while we're checking the balance and
        // refreshing the stake.
        let now = self.env.now();
        let _neuron_lock = self.lock_neuron_for_command(
            nid.id,
            NeuronInFlightCommand {
                timestamp: now,
                command: Some(InFlightCommand::ClaimOrRefreshNeuron(
                    claim_or_refresh.clone(),
                )),
            },
        )?;

        // Get the balance of the neuron from the ledger canister.
        tla_log_locals! { neuron_id: nid.id };
        let balance = self.ledger.account_balance(account).await?;
        let min_stake = self.economics().neuron_minimum_stake_e8s;
        if balance.get_e8s() < min_stake {
            return Err(GovernanceError::new_with_message(
                ErrorType::InsufficientFunds,
                format!(
                    "Account does not have enough funds to refresh a neuron. \
                     Please make sure that account has at least {:?} e8s (was {:?} e8s)",
                    min_stake,
                    balance.get_e8s()
                ),
            ));
        }
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

        Ok(nid)
    }
```

**File:** rs/nns/governance/src/neuron/types.rs (L376-379)
```rust
        let stake_e8s = self.stake_e8s();
        let boost = dissolve_delay_bonus_multiplier(self.dissolve_delay_seconds(now_seconds))
            * age_bonus_multiplier(self.age_seconds(now_seconds));
        let mut potential_voting_power = Decimal::from(stake_e8s) * boost;
```

**File:** rs/nns/governance/src/neuron/types.rs (L988-999)
```rust
    /// Set the cached stake of this neuron to `updated_stake_e8s` and adjust
    /// this neuron's age to be the weighted average of the priorly cached
    /// and the added stakes. For example, if neuron N had staked 10 ICP aging
    /// since 3 years and 5 ICP has been added, then
    /// `N.update_stake_adjust_age(15 ICP)` will result in N staking 15 ICP aged
    /// at (10 ICP * 3 years) / (10 ICP + 5 ICP) = 2 years.
    ///
    /// Only a non-dissolving neuron has a non-zero age. The age of all other
    /// neurons (i.e., dissolving and dissolved) is represented as
    /// `aging_since_timestamp_seconds == u64::MAX`. This method maintains
    /// that invariant.
    pub fn update_stake_adjust_age(&mut self, updated_stake_e8s: u64, now: u64) {
```

**File:** rs/nns/governance/src/neuron/types.rs (L1021-1038)
```rust
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
```

**File:** rs/nns/governance/tests/governance.rs (L4658-4666)
```rust
// Test cases for claim and refresh
// - Claim with memo
//   - Controller
//   - Someone else for the controller
//   - It's not possible for someone else to claim for themselves
// - Refresh with memo
//   - Anyone can do it
// - Refresh with subaccount
//   - Anyone can do it
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
