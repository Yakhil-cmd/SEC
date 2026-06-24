### Title
NNS Governance `disburse_neuron` Unconditionally Clears `neuron_fees_e8s` Without Performing Ledger Burn When Fees Are Below Transaction Fee - (`rs/nns/governance/src/governance.rs`)

---

### Summary

In `disburse_neuron`, the NNS governance canister unconditionally zeroes `neuron_fees_e8s` and decrements `cached_neuron_stake_e8s` even when the ledger burn is skipped because `fees_amount_e8s <= transaction_fee_e8s`. The fee tokens remain in the neuron's subaccount on the ledger. After disbursing, the neuron owner can call `refresh_neuron` to re-sync the governance cache with the actual ledger balance, recovering tokens that should have been permanently burned.

---

### Finding Description

In `disburse_neuron` (`rs/nns/governance/src/governance.rs`), the burn of accumulated neuron fees is conditional on the fee amount exceeding the transaction fee:

```rust
if fees_amount_e8s > transaction_fee_e8s {
    let _result = self.ledger.transfer_funds(
        fees_amount_e8s, 0,
        Some(neuron_subaccount),
        governance_minting_account(),
        now,
    ).await?;
}
```

However, the governance state update that follows is **unconditional** — it always executes regardless of whether the burn happened:

```rust
self.with_neuron_mut(id, |neuron| {
    if neuron.cached_neuron_stake_e8s > fees_amount_e8s {
        neuron.cached_neuron_stake_e8s -= fees_amount_e8s;
    } else {
        neuron.cached_neuron_stake_e8s = 0;
    }
    neuron.neuron_fees_e8s = 0;   // ← always cleared
})
.expect("Expected the parent neuron to exist");
``` [1](#0-0) 

When `fees_amount_e8s <= transaction_fee_e8s`:
- No ledger burn is issued — the fee tokens remain in the neuron's subaccount.
- `neuron_fees_e8s` is set to `0` and `cached_neuron_stake_e8s` is decremented by `fees_amount_e8s`.
- The governance state now believes the fees were burned, but the ledger balance is `fees_amount_e8s` higher than `cached_neuron_stake_e8s`.

After the disburse transfer completes, `fees_amount_e8s` tokens are stranded in the neuron's subaccount. The owner can then call `refresh_neuron`, which queries the actual ledger balance and updates `cached_neuron_stake_e8s` to match:

```rust
Ordering::Less => {
    neuron.update_stake_adjust_age(balance.get_e8s(), now);
}
``` [2](#0-1) 

This re-inflates the neuron's cached stake by the amount that should have been burned, allowing a second `disburse_neuron` call to extract those tokens.

**Contrast with SNS governance**, which correctly gates the state update behind the burn condition — the neuron's `cached_neuron_stake_e8s` and `neuron_fees_e8s` are only updated if the ledger burn actually succeeded:

```rust
if max_burnable_fee > transaction_fee_e8s {
    // ledger burn ...
    neuron.cached_neuron_stake_e8s = neuron.cached_neuron_stake_e8s.saturating_sub(max_burnable_fee);
    neuron.neuron_fees_e8s = neuron.neuron_fees_e8s.saturating_sub(max_burnable_fee);
}
``` [3](#0-2) 

---

### Impact Explanation

A neuron owner whose accumulated `neuron_fees_e8s` is at or below `transaction_fee_e8s` (10,000 e8s / 0.0001 ICP) can recover tokens that the protocol intended to permanently burn. The fee tokens are not destroyed on the ICP ledger; they remain in the neuron's governance subaccount. After disbursing, the owner calls `refresh_neuron` to re-sync the governance cache, then disburses again to extract the recovered tokens. This breaks the ledger conservation invariant: tokens charged as governance penalties are not actually removed from circulation.

---

### Likelihood Explanation

The trigger condition (`neuron_fees_e8s <= transaction_fee_e8s = 10,000 e8s`) is reachable by any neuron that has accumulated fees exclusively from `ManageNeuron` proposals, whose per-proposal fee (`neuron_management_fee_per_proposal_e8s`) is configurable and can be set to a value at or below the transaction fee. [4](#0-3) 

Regular proposal rejection fees (`reject_cost_e8s`, default 1 ICP = 100,000,000 e8s) are far above the threshold, so those are unaffected. The vulnerability is limited to the small-fee edge case, making it low-to-medium likelihood but directly reachable by an unprivileged ingress caller with a dissolved neuron.

---

### Recommendation

Mirror the SNS governance pattern: gate the governance state update on whether the ledger burn was actually performed. Replace the unconditional state update block with:

```rust
if fees_amount_e8s > transaction_fee_e8s {
    // ledger burn (existing) ...
    self.with_neuron_mut(id, |neuron| {
        if neuron.cached_neuron_stake_e8s > fees_amount_e8s {
            neuron.cached_neuron_stake_e8s -= fees_amount_e8s;
        } else {
            neuron.cached_neuron_stake_e8s = 0;
        }
        neuron.neuron_fees_e8s = 0;
    }).expect("Expected the parent neuron to exist");
}
```

This ensures `neuron_fees_e8s` is only cleared when the corresponding ledger burn has been confirmed, keeping governance state and ledger state consistent.

---

### Proof of Concept

1. Configure `neuron_management_fee_per_proposal_e8s` ≤ 10,000 e8s.
2. Submit one or more `ManageNeuron` proposals so that `neuron.neuron_fees_e8s` accumulates to a value ≤ 10,000 e8s.
3. Dissolve the neuron and wait for it to reach `Dissolved` state.
4. Call `disburse_neuron`. The burn branch is skipped (`fees_amount_e8s <= transaction_fee_e8s`), but `neuron_fees_e8s` is set to 0 and `cached_neuron_stake_e8s` is decremented. The fee tokens remain in the neuron's ledger subaccount.
5. Call `refresh_neuron` (via `ClaimOrRefresh`). The ledger balance query returns a value higher than `cached_neuron_stake_e8s` by `fees_amount_e8s`, so `update_stake_adjust_age` re-inflates the cached stake. [5](#0-4) 

6. Call `disburse_neuron` a second time to extract the recovered `fees_amount_e8s` tokens that should have been burned.

### Citations

**File:** rs/nns/governance/src/governance.rs (L2046-2075)
```rust
        if fees_amount_e8s > transaction_fee_e8s {
            let now = self.env.now();
            tla_log_label!("DisburseNeuron_Fee");
            tla_log_locals! {
                fees_amount: fees_amount_e8s,
                neuron_id: id.id,
                to_account: tla::account_to_tla(to_account),
                disburse_amount: disburse_amount_e8s
            };
            let _result = self
                .ledger
                .transfer_funds(
                    fees_amount_e8s,
                    0, // Burning transfers don't pay a fee.
                    Some(neuron_subaccount),
                    governance_minting_account(),
                    now,
                )
                .await?;
        }

        self.with_neuron_mut(id, |neuron| {
            // Update the stake and the fees to reflect the burning above.
            if neuron.cached_neuron_stake_e8s > fees_amount_e8s {
                neuron.cached_neuron_stake_e8s -= fees_amount_e8s;
            } else {
                neuron.cached_neuron_stake_e8s = 0;
            }
            neuron.neuron_fees_e8s = 0;
        })
```

**File:** rs/nns/governance/src/governance.rs (L5566-5569)
```rust
        match *action {
            Action::ManageNeuron(_) => Ok(self.economics().neuron_management_fee_per_proposal_e8s),
            _ => Ok(self.economics().reject_cost_e8s),
        }
```

**File:** rs/nns/governance/src/governance.rs (L5900-5961)
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
```

**File:** rs/sns/governance/src/governance.rs (L1181-1208)
```rust
        if max_burnable_fee > transaction_fee_e8s {
            let _result = self
                .ledger
                .transfer_funds(
                    max_burnable_fee,
                    0, // Burning transfers don't pay a fee.
                    Some(from_subaccount),
                    self.governance_minting_account(),
                    self.env.now(),
                )
                .await?;

            // We only update the cached_neuron_stake_e8s and neuron_fees_e8s if we actually
            // burn fees, otherwise this leads to ledger and governance getting out of sync.
            let nid = id.to_string();
            let neuron = self
                .proto
                .neurons
                .get_mut(&nid)
                .expect("Expected the parent neuron to exist");

            // Update the neuron's stake and management fees to reflect the burning
            // above.
            neuron.cached_neuron_stake_e8s = neuron
                .cached_neuron_stake_e8s
                .saturating_sub(max_burnable_fee);

            neuron.neuron_fees_e8s = neuron.neuron_fees_e8s.saturating_sub(max_burnable_fee);
```
