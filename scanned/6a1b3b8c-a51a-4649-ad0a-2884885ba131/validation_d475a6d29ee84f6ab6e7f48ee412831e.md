### Title
Accumulated Maturity of Donor GTC Neurons Is Permanently Lost During `transfer_gtc_neuron` - (`rs/nns/governance/src/governance.rs`)

---

### Summary

`Governance::transfer_gtc_neuron` migrates only the ICP stake (`cached_neuron_stake_e8s`) from a donor GTC neuron to a recipient neuron, then permanently deletes the donor. Any `maturity_e8s_equivalent` or `staked_maturity_e8s_equivalent` accumulated by the donor neuron is silently discarded and is unrecoverable. There is no mechanism for the recipient neuron's controller to claim the lost maturity. This is the direct IC analog of the reported Solidity pattern where `_resolvedPay` is written for a new address but no claim path exists.

---

### Finding Description

`Governance::transfer_gtc_neuron` is called by the GTC canister whenever a GTC account owner calls `donate_account`, or when anyone calls `forward_whitelisted_unclaimed_accounts` after the 188-day window. The function reads only `cached_neuron_stake_e8s` from the donor neuron, performs a ledger transfer of that stake to the recipient's subaccount, then deletes the donor neuron entirely:

```rust
// rs/nns/governance/src/governance.rs  lines 1873-1918
let (is_donor_controlled_by_gtc, donor_subaccount, donor_cached_neuron_stake_e8s) = self
    .with_neuron(donor_neuron_id, |donor_neuron| {
        // Only stake is read; maturity_e8s_equivalent and
        // staked_maturity_e8s_equivalent are never read.
        let donor_cached_neuron_stake_e8s = donor_neuron.cached_neuron_stake_e8s;
        (is_donor_controlled_by_gtc, donor_subaccount, donor_cached_neuron_stake_e8s)
    })?;
// ...
let donor_neuron = self.with_neuron(donor_neuron_id, |neuron| neuron.clone())?;
self.remove_neuron(donor_neuron)?;          // donor deleted; maturity gone

self.with_neuron_mut(recipient_neuron_id, |recipient_neuron| {
    recipient_neuron.cached_neuron_stake_e8s += transfer_amount_doms; // only stake
})?;
``` [1](#0-0) 

By contrast, `merge_neurons` — the other neuron-consolidation path — explicitly transfers both `maturity_e8s_equivalent` and `staked_maturity_e8s_equivalent` to the target neuron:

```rust
// rs/nns/governance/src/governance/merge_neurons.rs  lines 69-85
pub fn source_effect(&self) -> MergeNeuronsSourceEffect {
    MergeNeuronsSourceEffect {
        subtract_maturity: self.transfer_maturity_e8s,
        subtract_staked_maturity: self.transfer_staked_maturity_e8s,
        ...
    }
}
pub fn target_effect(&self) -> MergeNeuronsTargetEffect {
    MergeNeuronsTargetEffect {
        add_maturity: self.transfer_maturity_e8s,
        add_staked_maturity: self.transfer_staked_maturity_e8s,
        ...
    }
}
``` [2](#0-1) 

The GTC canister's `AccountState::transfer` iterates over all neuron IDs and calls `transfer_gtc_neuron` for each one, with no maturity preservation step: [3](#0-2) 

The GTC canister exposes two public entry points that trigger this path:

- `donate_account` — callable by any authenticated GTC account owner who has not yet claimed, donated, or forwarded.
- `forward_whitelisted_unclaimed_accounts` — callable by **anyone** after 188 days post-genesis. [4](#0-3) [5](#0-4) 

---

### Impact Explanation

GTC neurons were created at genesis under the GTC canister's control and inherit the NNS default followees, meaning they vote via liquid democracy and accumulate `maturity_e8s_equivalent` over time. When `donate_account` or `forward_whitelisted_unclaimed_accounts` is invoked, every donor neuron's accumulated maturity is permanently destroyed — it is neither credited to the recipient neuron nor stored anywhere for later retrieval. This is a **ledger conservation bug**: maturity that represents real future ICP minting rights is silently annihilated. The recipient neuron's controller has no function to call to recover it.

---

### Likelihood Explanation

- `donate_account` is callable by any GTC account owner at any time after the 3-day genesis lock.
- `forward_whitelisted_unclaimed_accounts` is callable by any unprivileged principal after 188 days post-genesis (already elapsed since May 2021).
- GTC neurons have been live since genesis and have accumulated maturity through default followee voting for years.
- No special privilege, key compromise, or governance majority is required.

---

### Recommendation

In `transfer_gtc_neuron`, read `maturity_e8s_equivalent` and `staked_maturity_e8s_equivalent` from the donor neuron before deletion and add them to the recipient neuron, mirroring the `merge_neurons` pattern:

```rust
// After the ledger transfer succeeds, before remove_neuron:
let (donor_maturity, donor_staked_maturity) = self
    .with_neuron(donor_neuron_id, |n| {
        (n.maturity_e8s_equivalent, n.staked_maturity_e8s_equivalent.unwrap_or(0))
    })?;

let donor_neuron = self.with_neuron(donor_neuron_id, |neuron| neuron.clone())?;
self.remove_neuron(donor_neuron)?;

self.with_neuron_mut(recipient_neuron_id, |recipient_neuron| {
    recipient_neuron.cached_neuron_stake_e8s += transfer_amount_doms;
    recipient_neuron.maturity_e8s_equivalent =
        recipient_neuron.maturity_e8s_equivalent.saturating_add(donor_maturity);
    recipient_neuron.staked_maturity_e8s_equivalent = Some(
        recipient_neuron.staked_maturity_e8s_equivalent.unwrap_or(0)
            .saturating_add(donor_staked_maturity),
    );
})?;
```

---

### Proof of Concept

1. A GTC account owner (or anyone after 188 days for whitelisted accounts) calls `donate_account` / `forward_whitelisted_unclaimed_accounts` on the GTC canister (`rs/nns/gtc/canister/canister.rs`).
2. The GTC canister calls `AccountState::transfer`, which loops over `neuron_ids` and calls `GovernanceCanister::transfer_gtc_neuron(neuron_id, custodian_neuron_id)` for each. [6](#0-5) 
3. The NNS Governance canister executes `Governance::transfer_gtc_neuron`. It reads only `cached_neuron_stake_e8s`, performs the ledger transfer, calls `self.remove_neuron(donor_neuron)`, and increments only `recipient_neuron.cached_neuron_stake_e8s`. [1](#0-0) 
4. The donor neuron's `maturity_e8s_equivalent` (potentially years of accumulated voting rewards) is destroyed with the neuron. The recipient neuron's controller has no method to claim it. There is no `_resolvedPay`-equivalent mapping and no claim function — the value is simply gone.

### Citations

**File:** rs/nns/governance/src/governance.rs (L1873-1918)
```rust
        let (is_donor_controlled_by_gtc, donor_subaccount, donor_cached_neuron_stake_e8s) = self
            .with_neuron(donor_neuron_id, |donor_neuron| {
                let is_donor_controlled_by_gtc =
                    donor_neuron.controller() == *GENESIS_TOKEN_CANISTER_ID.get_ref();
                let donor_subaccount = donor_neuron.subaccount();
                let donor_cached_neuron_stake_e8s = donor_neuron.cached_neuron_stake_e8s;
                (
                    is_donor_controlled_by_gtc,
                    donor_subaccount,
                    donor_cached_neuron_stake_e8s,
                )
            })?;
        let recipient_subaccount = self.with_neuron(recipient_neuron_id, |recipient_neuron| {
            recipient_neuron.subaccount()
        })?;

        if !is_donor_controlled_by_gtc {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                "Donor neuron is not controlled by the GTC",
            ));
        }

        let transaction_fee = self.transaction_fee();

        let recipient_account_identifier = neuron_subaccount(recipient_subaccount);

        let transfer_amount_doms = donor_cached_neuron_stake_e8s - transaction_fee;

        let _ = self
            .ledger
            .transfer_funds(
                transfer_amount_doms,
                transaction_fee,
                Some(donor_subaccount),
                recipient_account_identifier,
                0,
            )
            .await?;

        let donor_neuron = self.with_neuron(donor_neuron_id, |neuron| neuron.clone())?;
        self.remove_neuron(donor_neuron)?;

        self.with_neuron_mut(recipient_neuron_id, |recipient_neuron| {
            recipient_neuron.cached_neuron_stake_e8s += transfer_amount_doms;
        })?;
```

**File:** rs/nns/governance/src/governance/merge_neurons.rs (L69-85)
```rust
    pub fn source_effect(&self) -> MergeNeuronsSourceEffect {
        MergeNeuronsSourceEffect {
            dissolve_state_and_age: self.source_neuron_dissolve_state_and_age,
            subtract_maturity: self.transfer_maturity_e8s,
            subtract_staked_maturity: self.transfer_staked_maturity_e8s,
            subtract_eight_year_gang_bonus_base_e8s: self.transfer_eight_year_gang_bonus_base_e8s,
        }
    }

    pub fn target_effect(&self) -> MergeNeuronsTargetEffect {
        MergeNeuronsTargetEffect {
            dissolve_state_and_age: self.target_neuron_dissolve_state_and_age,
            add_maturity: self.transfer_maturity_e8s,
            add_staked_maturity: self.transfer_staked_maturity_e8s,
            add_eight_year_gang_bonus_base_e8s: self.transfer_eight_year_gang_bonus_base_e8s,
        }
    }
```

**File:** rs/nns/gtc/src/lib.rs (L75-93)
```rust
    pub async fn donate_account(
        &mut self,
        caller: &PrincipalId,
        public_key_hex: String,
    ) -> Result<(), String> {
        let public_key = decode_hex_public_key(&public_key_hex)?;
        validate_public_key_against_caller(&public_key, caller)?;

        let custodian_neuron_id = self.donate_account_recipient_neuron_id;

        let address = public_key_to_gtc_address(&public_key);
        let account = self.get_account_mut(&address)?;
        account.authenticated_principal_id = Some(*caller);

        account.transfer(custodian_neuron_id).await?;
        account.has_donated = true;

        Ok(())
    }
```

**File:** rs/nns/gtc/src/lib.rs (L95-130)
```rust
    /// Forwards the stake of whitelisted GTC neurons that have not been claimed
    /// (or donated) to the Neuron given by
    /// `self.forward_whitelisted_unclaimed_accounts_recipient_neuron_id`.
    ///
    /// This method will be allowed to be called by anyone after
    /// `SECONDS_UNTIL_FORWARD_WHITELISTED_UNCLAIMED_ACCOUNTS_CAN_BE_CALLED` has
    /// elapsed.
    pub async fn forward_whitelisted_unclaimed_accounts(&mut self) -> Result<(), String> {
        self.assert_forward_whitelisted_unclaimed_accounts_can_be_called()?;
        let mut forward_whitelist = HashSet::new();

        for gtc_address in &self.whitelisted_accounts_to_forward {
            forward_whitelist.insert(gtc_address.to_string());
        }

        let custodian_neuron_id = self.forward_whitelisted_unclaimed_accounts_recipient_neuron_id;

        for (gtc_address, account) in self.accounts.iter_mut() {
            if !account.has_claimed
                && !account.has_donated
                && !account.has_forwarded
                && forward_whitelist.contains(gtc_address)
            {
                match account.transfer(custodian_neuron_id).await {
                    Ok(_) => account.has_forwarded = true,
                    Err(error) => {
                        println!(
                            "Error forwarding gtc account: {}. Error: {}",
                            gtc_address, error
                        );
                    }
                }
            }
        }

        Ok(())
```

**File:** rs/nns/gtc/src/lib.rs (L186-210)
```rust
        let neuron_ids = self.neuron_ids.clone();

        for neuron_id in neuron_ids {
            let result =
                GovernanceCanister::transfer_gtc_neuron(neuron_id, custodian_neuron_id).await;

            self.neuron_ids.retain(|id| id != &neuron_id);

            let mut donated_neuron = TransferredNeuron {
                neuron_id: Some(neuron_id),
                timestamp_seconds: now_secs(),
                error: None,
            };

            match result {
                Ok(_) => self.successfully_transferred_neurons.push(donated_neuron),
                Err(e) => {
                    donated_neuron.error = Some(e.to_string());
                    self.failed_transferred_neurons.push(donated_neuron)
                }
            }
        }

        Ok(())
    }
```
