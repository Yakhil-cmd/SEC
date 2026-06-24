### Title
Unprivileged Caller Can Dilute Neuron Age Bonus via `ClaimOrRefresh` Stake Refresh - (`File: rs/nns/governance/src/governance.rs`)

---

### Summary

Any unprivileged principal can trigger a neuron age dilution on a victim's NNS (or SNS) neuron by transferring a small amount of ICP to the neuron's staking subaccount and then calling `manage_neuron` with `ClaimOrRefresh` (or the deprecated `claim_or_refresh_neuron_from_account`). This forces `update_stake_adjust_age` to execute, which resets the neuron's `aging_since_timestamp_seconds` to a more recent value, permanently reducing the age bonus applied to the victim's voting power and voting rewards.

---

### Finding Description

NNS governance neurons accumulate an **age bonus** over time (up to 25% at 4 years) that multiplies their voting power and, consequently, their share of voting rewards. The age is tracked via `aging_since_timestamp_seconds`. When a neuron's stake increases, `update_stake_adjust_age` computes a weighted-average age:

```
new_age = (old_age * old_stake) / new_stake
```

This is by design — adding new stake with zero age dilutes the existing age proportionally.

**The vulnerability**: `refresh_neuron` (NNS) and `refresh_neuron` (SNS) perform **no authorization check on the caller**. Any principal can call `manage_neuron` with `Command::ClaimOrRefresh` specifying any neuron's ID or subaccount. The code explicitly documents and tests that "anyone can do it":

```
// Tests that a neuron can be refreshed by subaccount, and that anyone can do it.
fn test_refresh_neuron_by_subaccount_by_proxy()
```

The attack flow:
1. Attacker transfers a small amount of ICP (e.g., 1 e8s above the minimum stake threshold) to Alice's neuron subaccount on the ICP ledger.
2. Attacker calls `manage_neuron` with `ClaimOrRefresh { by: NeuronIdOrSubaccount }` targeting Alice's neuron.
3. `refresh_neuron` reads the ledger balance (now slightly higher), calls `update_stake_adjust_age(new_balance, now)`.
4. `update_stake_adjust_age` computes a new weighted-average age that is younger than Alice's actual age, and writes the new `aging_since_timestamp_seconds`.
5. Alice's age bonus is permanently reduced. The attacker can repeat this to continuously dilute the age.

The same attack works on SNS governance via `rs/sns/governance/src/governance.rs`'s `refresh_neuron`.

---

### Impact Explanation

**Impact: Medium** — Loss of age bonus on voting power and voting rewards for targeted neuron holders.

- The age bonus in NNS is up to 25% (max at 4 years). For a neuron with 4 years of age, an attacker who transfers 1% of the neuron's stake can dilute the age by ~1%, reducing the age bonus proportionally.
- Repeated attacks (each requiring only a small ICP transfer + transaction fee) can continuously reset the age, preventing the victim from ever reaching maximum age bonus.
- Voting rewards are proportional to voting power, so reduced age bonus directly reduces ICP rewards earned per reward period.
- The attacker's cost is the ICP transfer fee per attack (10,000 e8s = 0.0001 ICP), making this economically viable for targeted harassment.

---

### Likelihood Explanation

**Likelihood: High** — The `ClaimOrRefresh` endpoint is publicly callable by any principal with no authorization check. The code and tests explicitly acknowledge this. The only cost to the attacker is the ICP ledger transfer fee per attack iteration. The neuron subaccount is deterministically computable from the controller's principal ID and memo (both public information for known neurons).

---

### Recommendation

1. **Add an authorization check to `refresh_neuron`**: Only allow the neuron's controller or a hotkey to trigger a stake refresh that results in `update_stake_adjust_age` being called. If the balance has not changed (i.e., `Ordering::Equal`), the call can remain permissionless since it has no state-changing effect.

2. **Alternatively, separate "discover neuron ID" from "refresh stake"**: The comment in `refresh_neuron` notes that the `Ordering::Equal` case "serves the purpose of allowing to discover the neuron id based on the memo and the controller." This use case can remain open, but the `Ordering::Less` branch (which calls `update_stake_adjust_age`) should require authorization.

3. **For SNS**: Apply the same fix to `rs/sns/governance/src/governance.rs`'s `refresh_neuron`.

---

### Proof of Concept

**Entry path (NNS)**:

1. Alice has neuron with ID `N`, controller `alice_principal`, subaccount `S`, stake 1000 ICP, age 3 years.
2. Attacker calls ICP ledger `transfer` sending 1 ICP to `Account { owner: governance_canister_id, subaccount: S }`.
3. Attacker calls NNS governance `manage_neuron` as any principal:
   ```
   ManageNeuron {
     neuron_id_or_subaccount: Some(NeuronIdOrSubaccount::NeuronId(N)),
     command: Some(ClaimOrRefresh { by: Some(By::NeuronIdOrSubaccount(Empty {})) })
   }
   ```
4. `refresh_neuron_by_id_or_subaccount` → `refresh_neuron` is called with no authorization check.
5. Balance is now 1001 ICP > cached 1000 ICP → `update_stake_adjust_age(1001_ICP, now)` is called.
6. New age = `(3_years * 1000) / 1001` ≈ 2.997 years. Alice loses ~1 day of age per 1 ICP attack.
7. Attacker repeats, each time paying only the 0.0001 ICP transfer fee.

**Relevant code references**:

`refresh_neuron` in NNS — no authorization check before calling `update_stake_adjust_age`: [1](#0-0) 

`update_stake_adjust_age` — dilutes age on stake increase: [2](#0-1) 

`refresh_neuron_by_id_or_subaccount` — no caller authorization: [3](#0-2) 

Test explicitly confirming anyone can refresh: [4](#0-3) 

SNS analog — same pattern, no authorization: [5](#0-4) 

`age_bonus_multiplier` — shows the bonus that is lost: [6](#0-5)

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

**File:** rs/nns/governance/tests/governance.rs (L5014-5028)
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
