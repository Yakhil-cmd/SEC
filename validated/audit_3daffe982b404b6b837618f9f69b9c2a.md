### Title
Permissionless `ClaimOrRefresh` Enables Forced Neuron Age Dilution via Unsolicited Stake Top-Up - (File: `rs/nns/governance/src/governance.rs`)

---

### Summary

Any unprivileged caller can force a victim's NNS neuron to absorb an unsolicited ICP top-up and then trigger `ClaimOrRefresh` on that neuron without authorization. Because `refresh_neuron` unconditionally calls `update_stake_adjust_age` on any inflow — regardless of who initiated the transfer — the neuron's `aging_since_timestamp_seconds` is diluted using a weighted average where the attacker-injected stake carries age 0. This permanently reduces the victim's age bonus and their proportional share of voting rewards for the lifetime of the neuron.

---

### Finding Description

`manage_neuron_internal` in NNS Governance handles `ClaimOrRefresh` in an early-return block that executes **before** any authorization check:

```rust
// We run claim or refresh before we check whether a neuron exists because it
// may not in the case of the neuron being claimed
if let Some(Command::ClaimOrRefresh(claim_or_refresh)) = &mgmt.command {
    // Note that we return here, so none of the rest of this method is executed
    // in this case.
    return match &claim_or_refresh.by {
        ...
        Some(By::NeuronIdOrSubaccount(_)) => {
            ...
            self.refresh_neuron_by_id_or_subaccount(id, claim_or_refresh)
                .await
                ...
        }
    };
}
``` [1](#0-0) 

`refresh_neuron_by_id_or_subaccount` resolves the neuron by ID or subaccount and calls `refresh_neuron` with no caller-identity check: [2](#0-1) 

Inside `refresh_neuron`, whenever the ledger balance exceeds the cached stake (i.e., an unsolicited deposit has arrived), `update_stake_adjust_age` is called unconditionally:

```rust
Ordering::Less => {
    neuron.update_stake_adjust_age(balance.get_e8s(), now);
}
``` [3](#0-2) 

`update_stake_adjust_age` computes a weighted-average age where the injected stake carries age 0:

```rust
let (new_stake_e8s, new_age_seconds) = combine_aged_stakes(
    self.cached_neuron_stake_e8s,
    self.age_seconds(now),
    updated_stake_e8s.saturating_sub(self.cached_neuron_stake_e8s),
    0,   // ← injected stake has age 0
);
``` [4](#0-3) 

The neuron's `aging_since_timestamp_seconds` is then advanced forward, permanently reducing the age bonus used in voting-power and reward calculations: [5](#0-4) 

The age bonus feeds directly into `potential_and_deciding_voting_power`:

```rust
let boost = dissolve_delay_bonus_multiplier(self.dissolve_delay_seconds(now_seconds))
    * age_bonus_multiplier(self.age_seconds(now_seconds));
let mut potential_voting_power = Decimal::from(stake_e8s) * boost;
``` [6](#0-5) 

The same structural pattern exists in SNS Governance: `refresh_neuron` at `rs/sns/governance/src/governance.rs` line 4237 also has no authorization check and calls `neuron.update_stake` unconditionally on any inflow. [7](#0-6) 

---

### Impact Explanation

A neuron's age bonus provides up to 25 % extra voting power (for neurons aged ≥ 4 years). Voting rewards are distributed proportionally to `deciding_voting_power`, which is derived from `potential_voting_power` which includes the age bonus. By diluting the age, the attacker permanently reduces the victim's proportional share of all future voting rewards for the lifetime of the neuron. The dilution is proportional to the ratio of injected stake to existing stake: injecting an amount equal to the victim's current stake halves the age, cutting the age bonus roughly in half. The effect is irreversible within the same neuron — the age cannot be restored without a governance migration (as has happened historically with `ResetAging`/`RestoreAging` audit events). [8](#0-7) 

---

### Likelihood Explanation

The attack is fully permissionless and requires only two on-chain actions available to any principal:

1. An ICP ledger transfer to the victim's neuron subaccount (the subaccount is publicly derivable from the neuron's controller and memo, both of which are visible in the neuron's public `transfer` field).
2. A `manage_neuron` call with `ClaimOrRefresh { by: NeuronIdOrSubaccount }` targeting the victim's neuron ID.

The attacker's cost is the ICP transferred (which accrues to the victim's stake) plus the ledger fee. For a targeted attack against a high-value neuron (e.g., a known neuron with years of accumulated age), the cost is proportional to the victim's stake and the desired dilution magnitude. There is no rate limit or cooldown on `ClaimOrRefresh`. The victim has no mechanism to refuse the top-up or to prevent the subsequent refresh call.

---

### Recommendation

1. **Require authorization for `ClaimOrRefresh` on existing neurons**: When `By::NeuronIdOrSubaccount` is used to refresh an already-existing neuron (as opposed to claiming a new one), require the caller to be the neuron's controller or a hot key. New-neuron claiming (where the neuron does not yet exist) can remain permissionless.

2. **Separate claiming from refreshing**: Split `ClaimOrRefresh` into two distinct commands. The `Claim` path (new neuron) stays open; the `Refresh` path (existing neuron) requires the neuron owner's authorization.

3. **Alternatively, track age separately from stake**: Record the age-weighted stake at the time of the last authorized top-up and only update it on authorized inflows, similar to the `lateInflow`/`lateInflowEpoch` fix applied in the referenced commit `559b098`.

---

### Proof of Concept

```
// Attacker knows victim's neuron ID = V and its subaccount = S
// (derivable from public neuron data)

// Step 1: Send dust ICP to victim's neuron subaccount
icp_ledger.transfer({
    to: governance_canister_subaccount(S),
    amount: 1_000_000,   // 0.01 ICP
    fee: 10_000,
    memo: 0,
});

// Step 2: Trigger permissionless refresh — no authorization required
governance.manage_neuron({
    neuron_id_or_subaccount: NeuronId(V),
    command: ClaimOrRefresh {
        by: NeuronIdOrSubaccount {}
    }
});

// Result: refresh_neuron reads balance = old_stake + 0.01 ICP
//         update_stake_adjust_age is called with new_stake = old_stake + 0.01 ICP
//         new_age = (old_stake * old_age) / (old_stake + 0.01 ICP)
//         aging_since_timestamp_seconds is advanced forward
//         Victim's age bonus is permanently reduced
//
// Repeat with larger amounts to achieve significant dilution.
// E.g., injecting an amount equal to victim's stake halves the age.
```

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

**File:** rs/nns/governance/src/governance.rs (L6104-6147)
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
                None => Err(GovernanceError::new_with_message(
                    ErrorType::InvalidCommand,
                    "Need to provide a way by which to claim or refresh the neuron.",
                )),
            };
```

**File:** rs/nns/governance/src/neuron/types.rs (L377-379)
```rust
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

**File:** rs/sns/governance/src/governance.rs (L4274-4295)
```rust
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
```

**File:** rs/nns/governance/proto/ic_nns_governance/pb/v1/governance.proto (L2617-2643)
```text
    // Reset aging timestamps (https://forum.dfinity.org/t/icp-neuron-age-is-52-years/21261/26).
    ResetAging reset_aging = 2;
    // Restore aging timestamp that were incorrectly reset (https://forum.dfinity.org/t/restore-neuron-age-in-proposal-129394/29840).
    RestoreAging restore_aging = 3;
    // Normalize neuron dissolve state and age (https://forum.dfinity.org/t/simplify-neuron-state-age/30527)
    NormalizeDissolveStateAndAge normalize_dissolve_state_and_age = 4;
  }

  message ResetAging {
    // The neuron id whose aging was reset.
    fixed64 neuron_id = 1;

    // The aging_since_timestamp_seconds before reset.
    uint64 previous_aging_since_timestamp_seconds = 2;

    // The aging_since_timestamp_seconds after reset.
    uint64 new_aging_since_timestamp_seconds = 3;

    // Neuron's dissolve state at the time of reset.
    oneof neuron_dissolve_state {
      uint64 when_dissolved_timestamp_seconds = 4;
      uint64 dissolve_delay_seconds = 5;
    }

    // Neuron's stake at the time of reset.
    uint64 neuron_stake_e8s = 6;
  }
```
