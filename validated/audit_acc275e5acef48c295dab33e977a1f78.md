### Title
Unprivileged Caller Can Dilute Any Neuron's Age Bonus via Unauthorized `refresh_neuron` — (`rs/nns/governance/src/governance.rs`)

---

### Summary

The NNS Governance canister allows any unprivileged ingress sender to call `manage_neuron` with a `ClaimOrRefresh` command targeting any neuron by its ID or subaccount. There is no authorization check on the refresh path. An attacker who sends a small amount of ICP to a victim's neuron subaccount and then triggers a refresh causes `update_stake_adjust_age` to be called, which permanently dilutes the victim's neuron age. This is the direct IC analog of the Water contract bug: an attacker can manipulate another user's locked-position state without their consent.

---

### Finding Description

The `manage_neuron_internal` function in `rs/nns/governance/src/governance.rs` handles `ClaimOrRefresh` commands before any authorization check is performed:

```rust
// We run claim or refresh before we check whether a neuron exists because it
// may not in the case of the neuron being claimed
if let Some(Command::ClaimOrRefresh(claim_or_refresh)) = &mgmt.command {
    return match &claim_or_refresh.by {
        ...
        Some(By::NeuronIdOrSubaccount(_)) => {
            ...
            self.refresh_neuron_by_id_or_subaccount(id, claim_or_refresh)
                .await
                ...
        }
``` [1](#0-0) 

`refresh_neuron_by_id_or_subaccount` resolves the neuron and calls `refresh_neuron` with no caller identity check: [2](#0-1) 

Inside `refresh_neuron`, when the ledger balance exceeds the cached stake (which the attacker arranges by sending ICP to the neuron's subaccount), `update_stake_adjust_age` is called: [3](#0-2) 

`update_stake_adjust_age` computes a weighted-average age, permanently reducing `aging_since_timestamp_seconds`:

```
new_age = (old_stake * old_age) / (old_stake + added_stake)
``` [4](#0-3) 

This is intentional design for the neuron owner's own top-ups, but the absence of any authorization check means **any caller** can trigger it against **any neuron**. The test suite explicitly documents this as intended behavior:

```
// - Refresh with memo
//   - Anyone can do it
// - Refresh with subaccount
//   - Anyone can do it
``` [5](#0-4) 

The `By::MemoAndController` path also allows any caller to specify an arbitrary `controller` principal, targeting any neuron whose subaccount is derivable from a known principal and memo: [6](#0-5) 

---

### Impact Explanation

A neuron's age bonus contributes up to **25% additional voting power** and proportionally higher ICP staking rewards. The age is computed from `aging_since_timestamp_seconds`. Each attacker-triggered refresh with a small ICP top-up dilutes this age:

- Victim neuron: 10 ICP staked, 4 years old → full 25% age bonus
- Attacker sends 1 ICP to neuron subaccount, calls refresh
- New age = (10 × 4 years) / 11 ≈ 3.6 years → age bonus reduced
- Repeated attacks drive age toward zero

The victim's ICP stake increases (the attacker's ICP is locked in the victim's neuron), but the victim's relative voting power and reward share are permanently reduced. For large neuron holders (e.g., 100M ICP staked for 4+ years), the age bonus represents substantial governance influence and annual ICP rewards. An attacker can systematically reduce a target's influence in NNS governance.

The SNS governance `refresh_neuron` path has the same structure and the same absence of authorization: [7](#0-6) 

---

### Likelihood Explanation

The attack is reachable by any unprivileged ingress sender with no special role. The attacker must spend ICP (which is locked in the victim's neuron), but:

1. The ICP cost per dilution step is bounded by the minimum stake (`neuron_minimum_stake_e8s`, currently 1 ICP on mainnet).
2. The attack is permissionless and requires no coordination.
3. Neuron IDs and subaccounts are public (derivable from principal + memo, or queryable via `get_neuron_ids`).
4. The attack is economically rational against high-value targets where reducing a competitor's voting power is worth the ICP cost.

---

### Recommendation

Add an authorization check to the `ClaimOrRefresh` refresh path (distinct from the claim path, which legitimately needs to be open). Specifically, for `By::NeuronIdOrSubaccount` and `By::MemoAndController` when the neuron already exists, verify that the caller is the neuron's controller or a registered hot key before calling `refresh_neuron`. The claim path (neuron does not yet exist) can remain open, as the subaccount is derived from the controller's principal and the attacker cannot claim a neuron they do not control.

Alternatively, restrict `update_stake_adjust_age` so that age dilution only occurs when the caller is the neuron's controller or hot key, while still allowing balance synchronization for anyone (without the age side-effect).

---

### Proof of Concept

1. Victim `V` has neuron `N` with 10 ICP staked, 4 years old, dissolve delay 8 years (not dissolving). Full age bonus applies.

2. Attacker `A` (any principal) queries the NNS ledger to find `N`'s subaccount (public information).

3. `A` transfers 1 ICP to `N`'s subaccount on the ICP ledger. Ledger balance is now 11 ICP; cached stake is 10 ICP.

4. `A` calls `manage_neuron` on the NNS governance canister:
   ```
   ManageNeuron {
     neuron_id_or_subaccount: Some(NeuronId(N.id)),
     command: Some(ClaimOrRefresh { by: Some(NeuronIdOrSubaccount(())) })
   }
   ```

5. `refresh_neuron` is called. Balance (11 ICP) > cached stake (10 ICP) → `update_stake_adjust_age(11 ICP, now)` is called. New age = (10 × 4 years) / 11 ≈ 3.64 years. Age bonus reduced from 25% to ~22.7%.

6. Repeat steps 3–5 indefinitely. Each iteration costs `A` 1 ICP (locked in `V`'s neuron) and reduces `V`'s age bonus further. After ~40 iterations (40 ICP spent), `V`'s neuron has 50 ICP staked but age ≈ 0.8 years, age bonus ≈ 5% instead of 25%. [8](#0-7) [9](#0-8)

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

**File:** rs/nns/governance/src/governance.rs (L5900-5962)
```rust
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

**File:** rs/nns/governance/src/governance.rs (L6104-6142)
```rust
        // We run claim or refresh before we check whether a neuron exists because it
        // may not in the case of the neuron being claimed
        if let Some(Command::ClaimOrRefresh(claim_or_refresh)) = &mgmt.command {
            // Note that we return here, so none of the rest of this method is executed
            // in this case.
            return match &claim_or_refresh.by {
                Some(By::Memo(memo)) => {
                    let memo_and_controller = MemoAndController {
                        memo: *memo,
                        controller: None,
                    };
                    self.claim_or_refresh_neuron_by_memo_and_controller(
                        caller,
                        memo_and_controller,
                        claim_or_refresh,
                    )
                    .await
                    .map(ManageNeuronResponse::claim_or_refresh_neuron_response)
                }
                Some(By::MemoAndController(memo_and_controller)) => self
                    .claim_or_refresh_neuron_by_memo_and_controller(
                        caller,
                        memo_and_controller.clone(),
                        claim_or_refresh,
                    )
                    .await
                    .map(ManageNeuronResponse::claim_or_refresh_neuron_response),

                Some(By::NeuronIdOrSubaccount(_)) => {
                    let id = mgmt.get_neuron_id_or_subaccount()?.ok_or_else(|| {
                        GovernanceError::new_with_message(
                            ErrorType::NotFound,
                            "No neuron ID specified in the management request.",
                        )
                    })?;
                    self.refresh_neuron_by_id_or_subaccount(id, claim_or_refresh)
                        .await
                        .map(ManageNeuronResponse::claim_or_refresh_neuron_response)
                }
```

**File:** rs/nns/governance/src/neuron/types.rs (L999-1040)
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
    }
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

**File:** rs/sns/governance/src/governance.rs (L4237-4298)
```rust
    async fn refresh_neuron(&mut self, nid: &NeuronId) -> Result<(), GovernanceError> {
        let now = self.env.now();
        let subaccount = nid.subaccount()?;
        let account = self.neuron_account_id(subaccount);

        // First ensure that the neuron was not created via an NNS Neurons' Fund participation in the
        // decentralization swap
        {
            let neuron = self.get_neuron_result(nid)?;

            if neuron.is_neurons_fund_controlled() {
                return Err(GovernanceError::new_with_message(
                    ErrorType::PreconditionFailed,
                    "Cannot refresh an SNS Neuron controlled by the Neurons' Fund",
                ));
            }
        }

        // Get the balance of the neuron from the ledger canister.
        let balance = self.ledger.account_balance(account).await?;

        let min_stake = self
            .nervous_system_parameters_or_panic()
            .neuron_minimum_stake_e8s
            .expect("NervousSystemParameters must have neuron_minimum_stake_e8s");
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
        let neuron = self.get_neuron_result_mut(nid)?;
        match neuron.cached_neuron_stake_e8s.cmp(&balance.get_e8s()) {
            Ordering::Greater => {
                log!(
                    ERROR,
                    "ERROR. Neuron cached stake was inconsistent.\
                     Neuron account: {} has less e8s: {} than the cached neuron stake: {}.\
                     Stake adjusted.",
                    account,
                    balance.get_e8s(),
                    neuron.cached_neuron_stake_e8s
                );
                neuron.update_stake(balance.get_e8s(), now);
            }
            Ordering::Less => {
                neuron.update_stake(balance.get_e8s(), now);
            }
            // If the stake is the same as the account balance,
            // just return the neuron id (this way this method
            // also serves the purpose of allowing to discover the
            // neuron id based on the memo and the controller).
            Ordering::Equal => (),
        };

        Ok(())
    }
```
