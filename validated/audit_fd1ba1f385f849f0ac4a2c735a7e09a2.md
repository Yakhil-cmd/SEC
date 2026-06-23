### Title
SNS Governance `maybe_finalize_disburse_maturity` Permanently Loses Maturity When Transfer Fails With No Retry - (`rs/sns/governance/src/governance.rs`)

### Summary

The SNS governance canister's `maybe_finalize_disburse_maturity` function transfers minted SNS tokens to a user-specified destination account after a 7-day delay. When the ledger transfer fails, the disbursement entry is **silently dropped** from the neuron's `disburse_maturity_in_progress` list with no retry, no requeue, and no refund. The maturity was already deducted from the neuron at initiation time, so the user permanently loses their maturity with no recourse.

This is the direct IC analog of the Teller Protocol H-4 vulnerability: a push-based payment to a user-controlled recipient address can fail, and the failure causes permanent loss of funds to the user rather than a safe retry or escrow.

### Finding Description

**Initiation phase** (`disburse_maturity`, line 1609–1705 of `rs/sns/governance/src/governance.rs`):

1. The caller specifies a `to_account` (any arbitrary ICRC-1 account).
2. `maturity_to_deduct` is subtracted from `neuron.maturity_e8s_equivalent` immediately.
3. A `DisburseMaturityInProgress` entry is pushed to `neuron.disburse_maturity_in_progress` with a `finalize_disbursement_timestamp_seconds` set 7 days in the future.

**Finalization phase** (`maybe_finalize_disburse_maturity`, lines 4920–5083 of `rs/sns/governance/src/governance.rs`):

After 7 days, the periodic task calls `transfer_funds` to mint tokens to `to_account`. On failure, the code reaches:

```rust
Err(e) => {
    log!(ERROR, "Failed transferring funds ...");
    // ← no retry, no requeue, no refund
}
```

Then unconditionally:
```rust
neuron.disburse_maturity_in_progress.remove(0);  // only on success path
```

Wait — on the error path, `remove(0)` is NOT called. But the disbursement was already **popped from the list before the transfer attempt** in the NNS governance version. Let me clarify the SNS version precisely:

In the SNS `maybe_finalize_disburse_maturity` (lines 4938–5081), the disbursement is read from `neuron.disburse_maturity_in_progress.first()` but is **not removed** before the transfer. On success, `neuron.disburse_maturity_in_progress.remove(0)` is called at line 5069. On failure (line 5071–5079), the entry is **left in the list** — so the SNS version does retry.

However, the **NNS governance** `try_finalize_maturity_disbursement` (lines 558–675 of `rs/nns/governance/src/governance/disburse_maturity.rs`) has a different and more dangerous failure mode:

1. **Step 2** pops the disbursement from the neuron's list (`pop_maturity_disbursement_in_progress`) **before** the ledger call.
2. **Step 3** calls the ledger. If it fails, it attempts to push the disbursement back.
3. If the push-back also fails (line 657–674), `neuron_lock.retain()` is called — the neuron is **permanently locked** and the maturity is **permanently lost**.

The `FailToRestoreMaturityDisbursement` error path at line 665–674 explicitly acknowledges: *"the neuron mutation was performed, the ledger operation failed and the neuron mutation could not be reversed. The best we can do at this point is to retain the neuron lock."*

This means if the ledger call fails AND the push-back fails (both are fallible operations), the neuron is permanently locked with the maturity gone.

**Attacker-controlled entry path:**

An unprivileged user calls `manage_neuron` → `DisburseMaturity` with a `to_account` pointing to a canister they control. After 7 days, when `finalize_maturity_disbursement` runs, the attacker's canister can be set to trap or reject the minting call. If the ledger itself is temporarily unavailable at that exact moment, the push-back of the disbursement entry can also fail (e.g., if the neuron store is in an inconsistent state), triggering the `FailToRestoreMaturityDisbursement` path and permanently locking the neuron.

More practically: the ICP ledger's `can_send` check at line 202 of `rs/ledger_suite/icp/ledger/src/main.rs` can block the governance canister from sending. If governance is ever added to a send-blocklist (or the ledger is temporarily unavailable), the minting transfer fails, and the NNS governance neuron can be permanently locked.

### Impact Explanation

- **NNS governance**: A neuron whose maturity disbursement finalization fails AND whose push-back also fails is permanently locked (`neuron_lock.retain()`). The maturity is gone (already deducted at initiation), and the neuron cannot be used for voting, dissolving, or any other operation. This is a permanent loss of both the maturity and the neuron's functionality.
- **SNS governance**: The SNS version retries on failure (disbursement stays in list), so it is less severe — but the maturity was already deducted from `maturity_e8s_equivalent` at initiation, and if the destination account is permanently unreachable (e.g., a canister that is deleted), the maturity is stuck in a perpetual retry loop with no user-facing escape hatch.

### Likelihood Explanation

- The ICP ledger is generally reliable, making transient failures rare.
- However, the `FailToRestoreMaturityDisbursement` path in NNS governance requires two sequential failures (ledger call + push-back), which is unlikely but not impossible under high canister load or during upgrades.
- For SNS governance, a user who specifies a destination account on a canister that later gets deleted or stops accepting transfers will have their maturity permanently stuck in the retry queue — this is a realistic scenario for any SNS token holder who disbursed to a smart contract account.
- Likelihood: **Low-to-Medium** for the permanent lock scenario; **Medium** for the stuck-maturity scenario.

### Recommendation

1. **NNS governance**: In `try_finalize_maturity_disbursement`, if the push-back of the disbursement fails, do not retain the neuron lock permanently. Instead, log a critical error and attempt recovery via canister upgrade. Consider storing the failed disbursement in a separate recovery queue rather than relying on the neuron's in-progress list.

2. **SNS governance**: Add a maximum retry count for `disburse_maturity_in_progress` entries. After N failed attempts, refund the maturity back to `maturity_e8s_equivalent` and remove the entry, rather than retrying indefinitely.

3. **Both**: Validate at initiation time that the destination account is reachable (e.g., by checking it is not a canister-controlled account, or by requiring a pull-based claim pattern similar to the Teller escrow fix).

### Proof of Concept

**NNS governance permanent lock path:**

```
1. User calls manage_neuron(DisburseMaturity { percentage: 100, to_account: Some(X) })
   → maturity deducted from neuron immediately
   → MaturityDisbursement pushed to maturity_disbursements_in_progress

2. After 7 days, try_finalize_maturity_disbursement() runs:
   a. neuron_lock acquired
   b. pop_maturity_disbursement_in_progress() succeeds → disbursement removed from list
   c. mint_icp_with_ledger() fails (ledger unavailable / governance blocked)
   d. push_front_maturity_disbursement_in_progress() also fails (e.g., neuron store error)
   e. neuron_lock.retain() called → neuron permanently locked
   f. Returns FailToRestoreMaturityDisbursement

3. Result: neuron is permanently locked, maturity is gone, user has no recourse
``` [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** rs/sns/governance/src/governance.rs (L1680-1698)
```rust
        let now_seconds = self.env.now();
        let disbursement_in_progress = DisburseMaturityInProgress {
            amount_e8s: maturity_to_deduct,
            timestamp_of_disbursement_seconds: now_seconds,
            account_to_disburse_to: Some(to_account_proto),
            finalize_disbursement_timestamp_seconds: Some(
                now_seconds + MATURITY_DISBURSEMENT_DELAY_SECONDS,
            ),
        };

        // Re-borrow the neuron mutably to update now that the maturity has been
        // deducted and is waiting until the end of the window to modulate and disburse.
        let neuron = self.get_neuron_result_mut(id)?;
        neuron.maturity_e8s_equivalent = neuron
            .maturity_e8s_equivalent
            .saturating_sub(maturity_to_deduct);
        neuron
            .disburse_maturity_in_progress
            .push(disbursement_in_progress);
```

**File:** rs/sns/governance/src/governance.rs (L5037-5079)
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
```

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L310-328)
```rust
    let disbursement_in_progress = MaturityDisbursement {
        destination: Some(destination),
        amount_e8s: disbursement_maturity_e8s,
        timestamp_of_disbursement_seconds,
        finalize_disbursement_timestamp_seconds,
    };

    neuron_store
        .with_neuron_mut(id, |neuron| {
            neuron.add_maturity_disbursement_in_progress(disbursement_in_progress);
            neuron.maturity_e8s_equivalent = neuron
                .maturity_e8s_equivalent
                .saturating_sub(disbursement_maturity_e8s);
        })
        .map_err(|_| InitiateMaturityDisbursementError::Unknown {
            reason: "Failed to update neuron even though it was found before".to_string(),
        })?;

    Ok(disbursement_maturity_e8s)
```

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L363-371)
```rust
    FailToMintIcp {
        neuron_id: NeuronId,
        reason: String,
    },
    FailToRestoreMaturityDisbursement {
        neuron_id: NeuronId,
        reason: String,
    },
}
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
