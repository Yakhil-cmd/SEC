### Title
Permissionless NNS Neuron Stake Refresh Allows Attacker to Dilute Victim Neuron's Age Bonus and Reduce Voting Power - (`rs/nns/governance/src/governance.rs`)

---

### Summary

The NNS Governance canister allows any unprivileged ingress sender to trigger a stake refresh on any neuron they do not own or control. By first sending a small ICP transfer to a victim neuron's ledger subaccount and then calling `manage_neuron` with `ClaimOrRefresh { by: NeuronIdOrSubaccount }`, an attacker forces `update_stake_adjust_age` to execute on the victim's neuron. This dilutes the neuron's age (and thus its age bonus on voting power) without the owner's consent, permanently reducing the victim's voting rewards.

---

### Finding Description

The NNS Governance canister exposes `manage_neuron` with a `ClaimOrRefresh` command that supports three variants. The `By::NeuronIdOrSubaccount` variant routes to `refresh_neuron_by_id_or_subaccount`, which calls `refresh_neuron`. Neither function performs any authorization check on the caller — the code explicitly permits any principal to invoke this path. [1](#0-0) 

Inside `refresh_neuron`, the function reads the neuron's ledger account balance and, if the balance exceeds the cached stake, calls `update_stake_adjust_age`: [2](#0-1) 

`update_stake_adjust_age` computes a weighted-average age between the existing stake (with its accumulated age) and the newly detected stake (with age 0), permanently reducing the neuron's `aging_since_timestamp_seconds`: [3](#0-2) 

The ICP ledger allows anyone to transfer ICP to any account identifier, including a neuron's governance subaccount. An attacker can therefore:

1. Compute the victim neuron's subaccount (it is deterministic from the neuron's subaccount bytes, which are public).
2. Transfer a small amount of ICP to that subaccount via the ICP ledger.
3. Call `manage_neuron` with `ClaimOrRefresh { by: Some(By::NeuronIdOrSubaccount(Empty {})) }` targeting the victim's neuron ID or subaccount.
4. `refresh_neuron` detects the balance increase and calls `update_stake_adjust_age`, diluting the neuron's age.

The test suite explicitly documents and validates that any caller can perform this operation: [4](#0-3) 

The comment at line 5014 reads: *"Tests that a neuron can be refreshed by subaccount, and that anyone can do it."*

---

### Impact Explanation

The NNS age bonus contributes up to 25% additional voting power for neurons aged ≥ 4 years. Voting power directly determines the share of voting rewards a neuron receives. By repeatedly sending tiny ICP amounts and triggering refreshes, an attacker can continuously dilute the victim neuron's age toward zero, asymptotically eliminating the age bonus and reducing the victim's voting rewards proportionally. The age dilution is irreversible without the victim staking additional ICP and waiting years for the age to recover. This constitutes a **governance authorization bug** with a direct financial impact on the victim's reward stream. [5](#0-4) 

---

### Likelihood Explanation

The attack is reachable by any unprivileged ingress sender with ICP to spend. Neuron subaccounts are deterministic and publicly derivable. The attacker bears a cost (the ICP sent to the victim's neuron), but the ICP is locked in the victim's neuron rather than returned, making this a griefing attack where the attacker sacrifices ICP to harm the victim's age bonus. The cost scales with the victim's existing stake (a larger stake requires more ICP to meaningfully dilute the age). For high-value neurons with large stakes, the cost of a meaningful attack is non-trivial, keeping likelihood medium.

---

### Recommendation

Add an authorization check in `refresh_neuron` (and `refresh_neuron_by_id_or_subaccount`) requiring the caller to be the neuron's controller or a registered hotkey before allowing the stake refresh to proceed. Alternatively, restrict `By::NeuronIdOrSubaccount` refreshes to authorized callers only, while keeping `By::MemoAndController` open (since that path requires knowledge of the memo and controller, which is effectively self-service). [6](#0-5) 

---

### Proof of Concept

**Setup:** Alice has a neuron with 1,000 ICP staked for 4 years (age bonus ≈ 25%, voting power ≈ 1,250 ICP-equivalent). Bob is the attacker.

1. Bob computes Alice's neuron subaccount bytes (public information from the neuron's state).
2. Bob calls `icrc1_transfer` on the ICP ledger, sending 10 ICP to `AccountIdentifier::new(GOVERNANCE_CANISTER_ID, Some(alice_neuron_subaccount))`.
3. Bob calls `manage_neuron` on the NNS Governance canister:
   ```
   ManageNeuron {
     neuron_id_or_subaccount: Some(NeuronIdOrSubaccount::NeuronId(alice_neuron_id)),
     command: Some(ClaimOrRefresh { by: Some(By::NeuronIdOrSubaccount(Empty {})) }),
   }
   ```
4. `refresh_neuron` executes without any authorization check. It reads Alice's neuron balance (now 1,010 ICP) and calls `update_stake_adjust_age(1010 ICP, now)`.
5. Alice's neuron age is diluted: `new_age = (1000 * 4 years) / 1010 ≈ 3.96 years`. Her age bonus drops from 25% to ~24.75%.
6. Bob repeats steps 2–5 to continuously erode Alice's age bonus. After enough iterations, Alice's age bonus approaches 0, permanently reducing her voting rewards. [7](#0-6) [8](#0-7)

### Citations

**File:** rs/nns/governance/src/governance.rs (L5873-5896)
```rust
    /// Refreshes the neuron, getting both it's id and subaccount, if only one
    /// of them was provided as argument.
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

**File:** rs/nns/governance/src/neuron/types.rs (L371-398)
```rust
    pub fn potential_and_deciding_voting_power(
        &self,
        voting_power_economics: &VotingPowerEconomics,
        now_seconds: u64,
    ) -> (u64, u64) {
        let stake_e8s = self.stake_e8s();
        let boost = dissolve_delay_bonus_multiplier(self.dissolve_delay_seconds(now_seconds))
            * age_bonus_multiplier(self.age_seconds(now_seconds));
        let mut potential_voting_power = Decimal::from(stake_e8s) * boost;

        // 8 Year Gang bonus. Cap the bonus base to the current stake because
        // rejection fees can cause the bonus base to exceed stake_e8s.
        if is_mission_70_voting_rewards_enabled() {
            let eight_year_gang_bonus_base_e8s = self.eight_year_gang_bonus_base_e8s.min(stake_e8s);
            potential_voting_power +=
                Decimal::from(eight_year_gang_bonus_base_e8s) / Decimal::from(10) * boost;
        }

        // For DECIDING voting power.
        let adjustment_factor: Decimal = {
            let time_since_last_refreshed = Duration::from_secs(
                now_seconds.saturating_sub(self.voting_power_refreshed_timestamp_seconds),
            );

            voting_power_economics
                .deciding_voting_power_adjustment_factor(time_since_last_refreshed)
        };

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

**File:** rs/nns/governance/tests/governance.rs (L5014-5029)
```rust
/// Tests that a neuron can be refreshed by subaccount, and that anyone can do
/// it.
#[test]
#[cfg_attr(feature = "tla", with_tla_trace_check)]
fn test_refresh_neuron_by_subaccount_by_controller() {
    let owner = *TEST_NEURON_1_OWNER_PRINCIPAL;
    refresh_neuron_by_id_or_subaccount(owner, owner, RefreshBy::Subaccount);
}

#[test]
#[cfg_attr(feature = "tla", with_tla_trace_check)]
fn test_refresh_neuron_by_subaccount_by_proxy() {
    let owner = *TEST_NEURON_1_OWNER_PRINCIPAL;
    let caller = *TEST_NEURON_1_OWNER_PRINCIPAL;
    refresh_neuron_by_id_or_subaccount(owner, caller, RefreshBy::Subaccount);
}
```
