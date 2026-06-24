### Title
Unpermissioned `ClaimOrRefresh` Neuron Age Dilution via Third-Party Stake Injection - (`File: rs/nns/governance/src/governance.rs`)

---

### Summary

Any unprivileged principal can call `manage_neuron` with `Command::ClaimOrRefresh { by: By::NeuronIdOrSubaccount }` for **any** neuron without authorization. If the caller first transfers ICP to the target neuron's subaccount, the subsequent `refresh_neuron` call triggers `update_stake_adjust_age`, which dilutes the neuron's `aging_since_timestamp_seconds` toward `now`. Because neuron age directly drives the age-bonus multiplier in voting power (up to 1.25× at 4 years for NNS), an attacker can continuously suppress a victim neuron's age bonus, reducing both its voting power and its share of voting rewards (maturity accrual).

---

### Finding Description

`manage_neuron_internal` in `rs/nns/governance/src/governance.rs` dispatches `ClaimOrRefresh` commands before any ownership check:

```rust
// L6104-6141 (governance.rs)
if let Some(Command::ClaimOrRefresh(claim_or_refresh)) = &mgmt.command {
    return match &claim_or_refresh.by {
        Some(By::NeuronIdOrSubaccount(_)) => {
            let id = mgmt.get_neuron_id_or_subaccount()?...;
            self.refresh_neuron_by_id_or_subaccount(id, claim_or_refresh)
                .await
                ...
        }
        ...
    };
}
``` [1](#0-0) 

`refresh_neuron` contains **no caller identity check**. It reads the ledger balance and, when `balance > cached_stake`, calls `update_stake_adjust_age`:

```rust
// L5936-5958 (governance.rs)
self.with_neuron_mut(&nid, |neuron| {
    match neuron.cached_neuron_stake_e8s.cmp(&balance.get_e8s()) {
        Ordering::Less => {
            neuron.update_stake_adjust_age(balance.get_e8s(), now);  // age diluted
        }
        ...
    };
})?;
``` [2](#0-1) 

`update_stake_adjust_age` computes a weighted-average age where the newly added tokens carry age = 0:

```rust
// L999-1038 (neuron/types.rs)
let (new_stake_e8s, new_age_seconds) = combine_aged_stakes(
    self.cached_neuron_stake_e8s,
    self.age_seconds(now),
    updated_stake_e8s.saturating_sub(self.cached_neuron_stake_e8s),
    0,   // <-- added tokens have zero age
);
let new_aging_since_timestamp_seconds = now.saturating_sub(new_age_seconds);
self.set_dissolve_state_and_age(
    self.dissolve_state_and_age().adjust_age(new_aging_since_timestamp_seconds)
);
``` [3](#0-2) 

The resulting age is `old_age × old_stake / (old_stake + added_stake)`. Each injection resets the age toward zero.

The age feeds directly into the voting power formula:

```rust
// L377-378 (neuron/types.rs)
let boost = dissolve_delay_bonus_multiplier(self.dissolve_delay_seconds(now_seconds))
    * age_bonus_multiplier(self.age_seconds(now_seconds));
``` [4](#0-3) 

The age bonus multiplier saturates at **1.25× at 4 years** (`MAX_NEURON_AGE_FOR_AGE_BONUS = 4 * ONE_YEAR_SECONDS`):

```rust
// L23-31 (neuron/voting_power.rs)
pub(crate) fn age_bonus_multiplier(age_seconds: u64) -> Decimal {
    let t = age_seconds / MAX_NEURON_AGE_FOR_AGE_BONUS;
    t / Decimal::from(4) + Decimal::from(1)  // 0.25*t + 1
}
``` [5](#0-4) 

The same pattern exists in SNS governance via `claim_or_refresh_neuron_by_memo_and_controller` (callable with any `controller` principal) and `update_stake` in `rs/sns/governance/src/neuron.rs`. [6](#0-5) 

---

### Impact Explanation

- A victim neuron that has accumulated 4 years of age (maximum 1.25× age bonus) can have its age halved by an attacker who sends an amount equal to the neuron's current stake to its subaccount and calls `ClaimOrRefresh`.
- Repeated injections keep the age near zero, permanently suppressing the age bonus.
- Reduced voting power means reduced maturity (voting rewards) accrued per reward round.
- For NNS neurons the maximum loss is 25% of voting power; for SNS neurons the `max_age_bonus_percentage` is governance-configurable and can be higher.
- The injected ICP is not destroyed — it becomes part of the victim's stake — but the victim cannot refuse the injection or prevent the age dilution.

---

### Likelihood Explanation

- The attack entry point is the public `manage_neuron` ingress endpoint, callable by any principal with no ICP balance requirement beyond the ledger transfer fee.
- The neuron subaccount is deterministic (`SHA-256(0x0c || "neuron-stake" || controller || memo)` for SNS; `compute_neuron_staking_subaccount(controller, memo)` for NNS), so any neuron whose controller and memo are known (e.g., from on-chain history) is targetable.
- The economic cost to the attacker scales with the desired impact: halving the age of a 100 ICP neuron requires sending 100 ICP. The attacker does not recover this ICP. This makes sustained griefing expensive, but a one-time significant dilution (e.g., against a governance-critical neuron before a key vote) is feasible.

---

### Recommendation

1. **Harvest/preserve age before stake update**: Before calling `update_stake_adjust_age`, record the current `aging_since_timestamp_seconds` and only update it if the caller is the neuron's controller or a hotkey. Third-party refreshes should update `cached_neuron_stake_e8s` without touching the age.
2. **Alternatively, gate `By::NeuronIdOrSubaccount` refresh on neuron ownership**: Require the caller to be the controller or a hotkey when using the `NeuronIdOrSubaccount` variant, since this variant is the only one that does not require knowledge of the memo (which is a weak ownership signal).
3. **SNS analog**: Apply the same fix to `refresh_neuron` in `rs/sns/governance/src/governance.rs`.

---

### Proof of Concept

```
// Attacker's steps (NNS):
// 1. Identify victim neuron N with controller P, memo M, stake S e8s, age A years.
// 2. Compute subaccount = compute_neuron_staking_subaccount(P, M).
// 3. Transfer S e8s to AccountIdentifier(GOVERNANCE_CANISTER_ID, subaccount).
//    Cost: S e8s + 0.0001 ICP fee.
// 4. Call manage_neuron({
//      neuron_id_or_subaccount: NeuronId(N),
//      command: ClaimOrRefresh { by: NeuronIdOrSubaccount({}) }
//    }) from any principal.
// Result: neuron N now has stake 2S, age A/2 (age bonus halved).
// Repeat step 3-4 to keep age near zero.
``` [7](#0-6) [8](#0-7)

### Citations

**File:** rs/nns/governance/src/governance.rs (L5873-5895)
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

**File:** rs/nns/governance/src/governance.rs (L6104-6141)
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
```

**File:** rs/nns/governance/src/neuron/types.rs (L377-378)
```rust
        let boost = dissolve_delay_bonus_multiplier(self.dissolve_delay_seconds(now_seconds))
            * age_bonus_multiplier(self.age_seconds(now_seconds));
```

**File:** rs/nns/governance/src/neuron/types.rs (L999-1038)
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

**File:** rs/sns/governance/src/neuron.rs (L647-679)
```rust
    /// Updates the stake of this neuron to `new_stake` and adjust this neuron's
    /// age accordingly
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
