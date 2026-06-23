### Title
Unprivileged Neuron Age Dilution via Permissionless `ClaimOrRefresh` Stake Refresh - (`rs/nns/governance/src/governance.rs`)

---

### Summary

Any unprivileged ingress sender can call `manage_neuron` with a `ClaimOrRefresh { by: NeuronIdOrSubaccount }` command targeting any victim neuron. By first sending a small ICP transfer to the victim's neuron subaccount and then triggering a stake refresh, the attacker causes `update_stake_adjust_age` to execute on the victim's neuron, diluting its `aging_since_timestamp_seconds` forward in time. This reduces the victim's age-based voting power bonus and proportionally reduces their voting rewards — a permissionless, sustained DoS on the neuron age reward mechanism.

---

### Finding Description

In `manage_neuron_internal`, the `ClaimOrRefresh` command branch is handled **before any authorization check** and returns early:

```rust
if let Some(Command::ClaimOrRefresh(claim_or_refresh)) = &mgmt.command {
    // Note that we return here, so none of the rest of this method is executed
    return match &claim_or_refresh.by {
        ...
        Some(By::NeuronIdOrSubaccount(_)) => {
            let id = mgmt.get_neuron_id_or_subaccount()?...;
            self.refresh_neuron_by_id_or_subaccount(id, claim_or_refresh)
                .await
                ...
        }
    };
}
``` [1](#0-0) 

`refresh_neuron_by_id_or_subaccount` accepts any neuron ID or subaccount with no caller identity check: [2](#0-1) 

When the ledger balance of the neuron's subaccount exceeds its cached stake (because the attacker sent ICP there), `refresh_neuron` calls `update_stake_adjust_age`: [3](#0-2) 

`update_stake_adjust_age` computes a weighted average of old age and new stake, advancing `aging_since_timestamp_seconds` toward the present:

```rust
let (new_stake_e8s, new_age_seconds) = combine_aged_stakes(
    self.cached_neuron_stake_e8s,
    self.age_seconds(now),
    updated_stake_e8s.saturating_sub(self.cached_neuron_stake_e8s),
    0,  // <-- added stake has age 0
);
let new_aging_since_timestamp_seconds = now.saturating_sub(new_age_seconds);
``` [4](#0-3) 

The same pattern exists in SNS governance's `refresh_neuron`, which calls `neuron.update_stake(balance.get_e8s(), now)` with no authorization check on the caller: [5](#0-4) 

The existing test suite explicitly documents and validates that **any caller** can refresh any neuron by proxy: [6](#0-5) 

---

### Impact Explanation

The neuron age bonus contributes up to **1.25× voting power** in NNS (linearly from 0 to 4 years): [7](#0-6) 

This age bonus feeds directly into `potential_and_deciding_voting_power`, which determines both voting weight and voting reward share: [8](#0-7) 

In SNS, `max_age_bonus_percentage` is configurable and can be set higher than NNS's 25%: [9](#0-8) 

Each attack dilutes the victim's age by the ratio `old_stake / (old_stake + added_stake)`. Repeated attacks compound this dilution. A neuron with 1 ICP stake that has accumulated 4 years of age (maximum bonus) can have its age halved by an attacker who sends 1 ICP to its subaccount and calls `ClaimOrRefresh`. The ICP is absorbed into the victim's stake (not returned to the attacker), making the attack costly but permissionless and sustained.

---

### Likelihood Explanation

- **Entry path**: Any unprivileged ingress sender can call `manage_neuron` on the NNS governance canister (`rrkah-fqaaa-aaaaa-aaaaq-cai`) with `ClaimOrRefresh { by: NeuronIdOrSubaccount }`. Neuron IDs and subaccounts are public.
- **Precondition**: The attacker must send ICP to the victim's neuron subaccount first. The cost per attack is proportional to the desired age dilution (e.g., to halve the age of a 1 ICP neuron costs 1 ICP + fees).
- **Constraint**: Unlike the EVM report's 1-wei attack, this attack has a real ICP cost. However, it is still permissionless, repeatable, and targets high-value long-aged neurons (which have the most to lose from age dilution).
- **Likelihood**: Medium — economically rational for targeted attacks on high-value neurons or SNS governance participants with large age bonuses.

---

### Recommendation

1. **Require caller authorization for `ClaimOrRefresh` when targeting an existing neuron by ID/subaccount**: Only the neuron's controller or a hotkey should be permitted to trigger a stake refresh that modifies `aging_since_timestamp_seconds`. Claiming a new neuron (where the neuron does not yet exist) can remain permissionless.

2. **Alternatively, only update age when the caller is the neuron controller**: In `refresh_neuron`, skip the `update_stake_adjust_age` call (or use a no-age-adjustment variant) when the caller is not the neuron's controller or hotkey.

3. **Minimum added-stake threshold**: Reject refresh calls where `balance - cached_stake < MINIMUM_REFRESH_DELTA` to prevent dust-level age dilution attacks.

---

### Proof of Concept

```
// Attacker (bob) targets victim (alice) who has a 4-year-old neuron with 1 ICP staked.
// Step 1: Bob sends 1 ICP to alice's neuron subaccount on the ICP ledger.
//   alice_neuron_subaccount = compute_neuron_staking_subaccount(alice_controller, alice_memo)
//   ledger.transfer(to: governance_canister/alice_neuron_subaccount, amount: 1 ICP)
//
// Step 2: Bob calls manage_neuron with ClaimOrRefresh targeting alice's neuron.
//   governance.manage_neuron(caller=bob, ManageNeuron {
//     neuron_id_or_subaccount: Some(NeuronId(alice_neuron_id)),
//     command: Some(ClaimOrRefresh { by: Some(NeuronIdOrSubaccount(())) })
//   })
//
// Result: alice's neuron age is halved (from 4 years to 2 years),
//         reducing her age bonus from 1.25x to 1.125x,
//         and her voting rewards are proportionally reduced.
//
// Bob can repeat this attack to continuously suppress alice's age bonus.
```

The `By::MemoAndController` path is equally exploitable: an attacker who knows the victim's controller principal and memo can trigger the same refresh by specifying them explicitly, without even knowing the neuron ID directly. [10](#0-9)

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

**File:** rs/nns/governance/src/neuron/types.rs (L376-379)
```rust
        let stake_e8s = self.stake_e8s();
        let boost = dissolve_delay_bonus_multiplier(self.dissolve_delay_seconds(now_seconds))
            * age_bonus_multiplier(self.age_seconds(now_seconds));
        let mut potential_voting_power = Decimal::from(stake_e8s) * boost;
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

**File:** rs/nns/governance/tests/governance.rs (L4922-4929)
```rust
/// Tests that a neuron can be refreshed by memo by proxy.
#[test]
#[cfg_attr(feature = "tla", with_tla_trace_check)]
fn test_refresh_neuron_by_memo_by_proxy() {
    let owner = *TEST_NEURON_1_OWNER_PRINCIPAL;
    let caller = *TEST_NEURON_2_OWNER_PRINCIPAL;
    refresh_neuron_by_memo(owner, caller);
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

**File:** rs/sns/governance/src/neuron.rs (L224-231)
```rust
        let a = std::cmp::min(self.age_seconds(now_seconds), max_neuron_age_for_age_bonus) as u128;
        let ad_stake = d_stake
            + if max_neuron_age_for_age_bonus > 0 {
                (d_stake * a * max_age_bonus_percentage as u128)
                    / (100 * max_neuron_age_for_age_bonus as u128)
            } else {
                0
            };
```
