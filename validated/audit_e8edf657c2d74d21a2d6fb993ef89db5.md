### Title
SNS Governance `maybe_finalize_disburse_maturity` Double-Mint After Successful Transfer — (`File: rs/sns/governance/src/governance.rs`)

---

### Summary

The SNS governance canister's `maybe_finalize_disburse_maturity` periodic task mints SNS tokens to a neuron owner **before** removing the disbursement entry from `disburse_maturity_in_progress`. If the minting transfer succeeds but the subsequent state-update step fails or is rolled back (e.g., due to a canister trap in the callback, or a canister upgrade that drops the in-flight callback), the disbursement entry remains in the queue and is re-minted on the next periodic task execution. This is the direct IC analog of the external report's "transfer succeeded → post-transfer verification fails → balance restored → double payout" pattern.

---

### Finding Description

`maybe_finalize_disburse_maturity` in `rs/sns/governance/src/governance.rs` (lines 4920–5082) iterates over all neurons whose `disburse_maturity_in_progress` entries are past their `finalize_disbursement_timestamp_seconds`. For each entry it:

1. Acquires a neuron lock.
2. Calls `self.ledger.transfer_funds(...)` — an async inter-canister minting call to the SNS ledger.
3. On `Ok(block_index)`: calls `self.get_neuron_result_mut(&neuron_id)` and then `neuron.disburse_maturity_in_progress.remove(0)`.
4. On `Err(e)`: logs the error and leaves the entry in the list for retry. [1](#0-0) 

The disbursement entry is **never removed before the ledger call**. The removal at line 5069 is only reached if both the ledger call and the subsequent `get_neuron_result_mut` succeed. Two failure modes leave the entry in the queue after a successful mint:

**Mode A — `get_neuron_result_mut` returns `Err`:** If the neuron cannot be found after the async call returns (e.g., due to a race with another message that mutated the neuron store), the `continue` at line 5066 skips the `remove(0)`, leaving the already-minted disbursement in the list. [2](#0-1) 

**Mode B — Canister trap / upgrade during callback:** In the IC execution model, if the callback traps after `transfer_funds` returns `Ok` but before `remove(0)` executes, the state changes from that callback are rolled back. The ledger mint is already committed (it is a separate canister's state), but the SNS governance state reverts to the pre-callback snapshot — with the disbursement entry still present. Similarly, if the SNS governance canister is upgraded while the `transfer_funds` inter-canister call is in flight, the callback is dropped and the disbursement entry survives in stable state.

The NNS governance canister's equivalent function, `try_finalize_maturity_disbursement`, explicitly avoids this by **popping the disbursement before calling the ledger** and pushing it back only on ledger failure: [3](#0-2) 

The SNS governance version has no such protection.

The `is_finalizing_disburse_maturity` flag prevents concurrent invocations of `maybe_finalize_disburse_maturity` within a single run, but it does not prevent re-execution across separate periodic task invocations after a rollback. [4](#0-3) 

---

### Impact Explanation

Each successful re-execution of the same `DisburseMaturityInProgress` entry mints a fresh batch of SNS tokens from the governance minting account to the neuron owner's account. The neuron's `maturity_e8s_equivalent` was already decremented when the disbursement was initiated (line 1693–1695), so the ledger's total supply grows by the disbursement amount on every duplicate mint. There is no protocol-side idempotency key on the minting call (the memo is `self.env.now()`, which changes on each invocation), so the ledger accepts each mint as a distinct transaction. The result is unbounded inflation of the SNS token supply proportional to the number of re-executions. [5](#0-4) 

---

### Likelihood Explanation

**Medium.** The most realistic trigger is a canister upgrade during an in-flight `transfer_funds` call. SNS governance canisters are upgraded via SNS proposals, which are routine. If an upgrade is executed while `maybe_finalize_disburse_maturity` has issued a minting call but not yet received the callback, the callback is dropped, the mint is committed on the ledger, and the disbursement entry survives in the upgraded canister's state. The next heartbeat re-mints the same amount. This requires no attacker: any SNS upgrade concurrent with a maturity disbursement finalization triggers the condition. The `get_neuron_result_mut` failure path (Mode A) is less likely in normal operation but is reachable if the neuron store is in an inconsistent state.

---

### Recommendation

Adopt the same pattern used by NNS governance's `try_finalize_maturity_disbursement`:

1. **Pop the disbursement entry before calling the ledger.** Remove the entry from `disburse_maturity_in_progress` atomically before the `transfer_funds` await.
2. **Push it back only on ledger failure.** If `transfer_funds` returns `Err`, re-insert the entry at the front of the list so it is retried.
3. **Retain the neuron lock if the push-back itself fails**, preventing the entry from being re-processed in an inconsistent state.

Additionally, use a stable idempotency key (e.g., a hash of `(neuron_id, disbursement_timestamp, amount)`) as the ledger memo so that a duplicate mint attempt is rejected by the ledger's deduplication window.

---

### Proof of Concept

1. SNS neuron owner calls `manage_neuron` → `DisburseMaturity { percentage_to_disburse: 100 }`. Maturity is deducted from `maturity_e8s_equivalent` and a `DisburseMaturityInProgress` entry is pushed to the neuron's list.
2. Seven days elapse. The SNS governance heartbeat calls `run_periodic_tasks` → `maybe_finalize_disburse_maturity`.
3. The function finds the entry, acquires the neuron lock, and calls `self.ledger.transfer_funds(amount, ...)` — an async inter-canister mint to the SNS ledger. The ledger commits the mint and returns `Ok(block_index)`.
4. **Before the callback executes `remove(0)`**, an SNS upgrade proposal is executed (or the callback traps due to any post-mint error). The SNS governance canister's state is rolled back to the pre-callback snapshot. The `DisburseMaturityInProgress` entry is still present; `maturity_e8s_equivalent` is still 0.
5. The next heartbeat calls `maybe_finalize_disburse_maturity` again. The entry's `finalize_disbursement_timestamp_seconds` is still in the past. The function mints the same amount again. The neuron owner's ledger balance increases by the disbursement amount a second time.
6. Steps 4–5 repeat for each subsequent upgrade or callback trap, with no upper bound on the number of duplicate mints. [1](#0-0) [6](#0-5)

### Citations

**File:** rs/sns/governance/src/governance.rs (L1692-1698)
```rust
        let neuron = self.get_neuron_result_mut(id)?;
        neuron.maturity_e8s_equivalent = neuron
            .maturity_e8s_equivalent
            .saturating_sub(maturity_to_deduct);
        neuron
            .disburse_maturity_in_progress
            .push(disbursement_in_progress);
```

**File:** rs/sns/governance/src/governance.rs (L4920-4935)
```rust
    // Disburses any maturity that should be disbursed, unless this is already happening.
    async fn maybe_finalize_disburse_maturity(&mut self) {
        if !self.can_finalize_disburse_maturity() {
            return;
        }

        let maturity_modulation_basis_points =
            match self.proto.effective_maturity_modulation_basis_points() {
                Ok(maturity_modulation_basis_points) => maturity_modulation_basis_points,
                Err(message) => {
                    log!(ERROR, "{}", message.error_message);
                    return;
                }
            };

        self.proto.is_finalizing_disburse_maturity = Some(true);
```

**File:** rs/sns/governance/src/governance.rs (L5037-5082)
```rust
            let transfer_result = self
                .ledger
                .transfer_funds(
                    maturity_to_disburse_after_modulation_e8s,
                    0,    // Minting transfers don't pay a fee.
                    None, // This is a minting transfer, no 'from' account is needed
                    to_account,
                    self.env.now(), // The memo(nonce) for the ledger's transaction
                )
                .await;
            match transfer_result {
                Ok(block_index) => {
                    log!(
                        INFO,
                        "Transferring DisburseMaturityInProgress-entry {:?} for neuron {} at block {}.",
                        disbursement,
                        neuron_id,
                        block_index
                    );
                    let neuron = match self.get_neuron_result_mut(&neuron_id) {
                        Ok(neuron) => neuron,
                        Err(e) => {
                            log!(
                                ERROR,
                                "Failed updating DisburseMaturityInProgress-entry {:?} for neuron {}: {}.",
                                disbursement,
                                neuron_id,
                                e
                            );
                            continue;
                        }
                    };
                    neuron.disburse_maturity_in_progress.remove(0);
                }
                Err(e) => {
                    log!(
                        ERROR,
                        "Failed transferring funds for DisburseMaturityInProgress-entry {:?} for neuron {}: {}.",
                        disbursement,
                        neuron_id,
                        e
                    );
                }
            }
        }
        self.proto.is_finalizing_disburse_maturity = None;
```

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L612-675)
```rust
    // Step 2: pop the maturity disbursement in progress. Since this is the first mutation, if it
    // fails, the neuron can still be unlocked as no mutations are performed yet. This is the main
    // thing the neuron lock is protecting against.
    let Ok(Some(maturity_disbursement_in_progress)) = governance.with_borrow_mut(|governance| {
        governance.with_neuron_mut(&neuron_id, |neuron| {
            neuron.pop_maturity_disbursement_in_progress()
        })
    }) else {
        // This should be impossible since we just checked that the disbursement exists in
        // `next_maturity_disbursement_to_finalize`.
        return Err(FinalizeMaturityDisbursementError::FailToPopMaturityDisbursement(neuron_id));
    };

    // Step 3: call ledger to perform the minting. If this fails, the neuron mutation needs to
    // be reversed.
    let account_identifier = destination
        .try_into_account_identifier()
        .map_err(|reason| FinalizeMaturityDisbursementError::AccountConversionFailure { reason })?;
    let mint_icp_operation = MintIcpOperation::new(account_identifier, amount_to_mint_e8s);
    let ledger = governance.with_borrow(|governance| governance.get_ledger());
    tla_log_locals! {
        neuron_id: neuron_id.id,
        current_disbursement: TlaValue::Record(BTreeMap::from(
            [
                ("account_id".to_string(), account_to_tla(account_identifier)),
                ("amount".to_string(), maturity_disbursement_in_progress.amount_e8s.to_tla_value()),
            ]
        ))
    };
    tla_log_label!("Disburse_Maturity_Timer");
    let mint_result = mint_icp_operation
        .mint_icp_with_ledger(ledger.as_ref(), now_seconds)
        .await;
    let Err(mint_error) = mint_result else {
        // Happy case: the minting was successful so we can exit here.
        return Ok(());
    };

    // Reaching this point means the minting failed and we need to reverse the neuron mutation
    // for consistency.
    let reverse_neuron_result = governance.with_borrow_mut(|governance| {
        governance.with_neuron_mut(&neuron_id, |neuron| {
            neuron.push_front_maturity_disbursement_in_progress(maturity_disbursement_in_progress);
        })
    });
    let Err(reverse_neuron_error) = reverse_neuron_result else {
        // The neuron mutation was successfully reversed and it will be re-tried later.
        return Err(FinalizeMaturityDisbursementError::FailToMintIcp {
            neuron_id,
            reason: mint_error.error_message,
        });
    };

    // Reaching this point means the neuron mutation was performed, the ledger operation failed
    // and the neuron mutation could not be reversed. The best we can do at this point is to
    // retain the neuron lock.
    neuron_lock.retain();
    Err(
        FinalizeMaturityDisbursementError::FailToRestoreMaturityDisbursement {
            neuron_id,
            reason: reverse_neuron_error.error_message,
        },
    )
}
```
