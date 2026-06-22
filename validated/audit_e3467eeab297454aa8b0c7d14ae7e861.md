### Title
State Mutation Before Inter-Canister Ledger Call Enables Permanent Neuron Lock and Maturity Loss - (File: `rs/nns/governance/src/governance/disburse_maturity.rs`)

---

### Summary

In the NNS Governance canister, `try_finalize_maturity_disbursement` pops the maturity disbursement from the neuron's queue (a committed state mutation) **before** the inter-canister ledger mint call. If the ledger call fails and the subsequent reversal also fails, the neuron is permanently locked and the user's maturity is irrecoverably lost. The TLA+ formal model for this function explicitly models the reversal failure as a non-deterministic outcome, confirming this is a known-possible execution path.

---

### Finding Description

In `rs/nns/governance/src/governance/disburse_maturity.rs`, the async function `try_finalize_maturity_disbursement` executes in this order:

**Step 2** (lines 615–623): The maturity disbursement entry is **popped** from the neuron's `maturity_disbursements_in_progress` queue — a committed state mutation — before any inter-canister call is made.

**Step 3** (lines 642–644): The inter-canister call to the ICP ledger to mint tokens is made (`await`). During this await, other ingress messages can be processed by the canister. At this point the neuron's disbursement queue shows the entry as gone, but the ICP has not yet been minted.

**Failure path** (lines 650–674): If the ledger call fails, the code attempts to reverse the pop by calling `push_front_maturity_disbursement_in_progress`. If **this reversal also fails** (e.g., `with_neuron_mut` returns an error), `neuron_lock.retain()` is called at line 668, permanently retaining the neuron lock. The neuron is then bricked: no further operations can be performed on it, and the maturity that was deducted when the disbursement was initiated is unrecoverable.

The TLA+ formal model at `rs/nns/governance/tla/Disburse_Maturity_Timer.tla` lines 62–68 explicitly models this non-deterministic failure:

```
if(answer.response = Variant("Fail", UNIT)) {
    either {
        neuron := [neuron EXCEPT ...]; (* reversal succeeds *)
        locks := locks \ {neuron_id};
    } or {
        skip  (* reversal fails — lock is NOT released *)
    }
}
```

The `skip` branch represents the path where the reversal fails and the lock is never released, confirming this is an acknowledged execution path in the formal model.

This is the direct IC analog of the Solidity checks-effects-interactions violation: state is mutated before the external call, creating a window of inconsistency and a failure path where the mutation cannot be undone.

---

### Impact Explanation

1. **Permanent neuron lock (DoS)**: If the ledger call fails and `with_neuron_mut` returns an error during reversal, `neuron_lock.retain()` is called. The neuron's `in_flight_commands` entry is never cleared. All subsequent operations on that neuron (`disburse`, `split`, `merge`, `stake_maturity`, etc.) will be rejected with `LedgerUpdateOngoing`. The neuron is permanently inaccessible without a canister upgrade to manually clear the lock.

2. **Maturity loss**: The maturity was already deducted from `neuron.maturity_e8s_equivalent` when `initiate_maturity_disbursement` was called. The disbursement entry that was popped in Step 2 is the only record of the pending ICP mint. If it cannot be restored, the user's maturity is gone and no ICP is ever minted.

3. **State inconsistency window**: During the ledger `await` (between Step 2 and the response), the neuron's disbursement queue is empty but ICP has not been minted. Query calls to the neuron during this window return a state that implies the disbursement completed, when it has not.

---

### Likelihood Explanation

- The ledger call can fail during normal operations: subnet upgrades, transient network partitions, or ledger canister being stopped for maintenance all produce a reject response.
- The reversal (`with_neuron_mut`) can fail if the neuron store returns an error for the neuron ID. While the neuron lock prevents deletion, the neuron store uses stable memory with fallible deserialization paths; a corruption or index inconsistency could cause `with_neuron_mut` to return `Err`.
- The TLA+ model explicitly includes the reversal-failure branch as a reachable non-deterministic outcome, meaning the DFINITY formal verification team considers it a real possibility.
- Any NNS neuron owner who calls `disburse_maturity` and whose neuron hits this path during ledger unavailability is affected. This is reachable by any unprivileged ingress sender who owns an NNS neuron with maturity.

---

### Recommendation

Apply the checks-effects-interactions pattern: do **not** pop the disbursement from the queue before the ledger call. Instead, keep the disbursement in the queue during the ledger call and only remove it after the ledger call succeeds. This is exactly the pattern used in the SNS governance analog (`rs/sns/governance/src/governance.rs` lines 5037–5069), where `neuron.disburse_maturity_in_progress.remove(0)` is called only inside the `Ok(block_index)` branch after the `await`.

Concretely, restructure `try_finalize_maturity_disbursement` so that:
1. Lock is acquired (Step 1 — unchanged)
2. Ledger mint call is made (Step 2 — moved before state mutation)
3. Only on success: pop the disbursement from the queue (Step 3 — moved after the await)

This eliminates the reversal logic entirely and removes the permanent-lock failure path.

---

### Proof of Concept

**Entry path**: Unprivileged NNS neuron owner.

1. User calls `manage_neuron` → `disburse_maturity` on their NNS neuron. `initiate_maturity_disbursement` deducts maturity and enqueues a `MaturityDisbursement` entry.

2. After 7 days, the governance timer fires `finalize_maturity_disbursement` → `try_finalize_maturity_disbursement`.

3. At line 615–623, `neuron.pop_maturity_disbursement_in_progress()` removes the entry from the queue. The neuron's state now shows 0 pending disbursements.

4. At line 642–644, `mint_icp_with_ledger(...).await` is called. The ICP ledger is temporarily unavailable (e.g., subnet upgrade in progress) and returns a reject.

5. At line 652–656, `governance.with_neuron_mut(&neuron_id, |neuron| { neuron.push_front_maturity_disbursement_in_progress(...) })` is called. If the neuron store returns an error (e.g., stable memory index inconsistency), this returns `Err`.

6. At line 668, `neuron_lock.retain()` is called. The `NeuronAsyncLock` is dropped with `retain = true`, so `unlock_neuron` is never called. The `in_flight_commands` map retains the entry for this neuron ID permanently.

7. All future `manage_neuron` calls for this neuron return `LedgerUpdateOngoing`. The maturity that was deducted in step 1 is unrecoverable.

**Key code references**: [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) 

**Contrast with correct SNS pattern** (state removed only after successful ledger call): [6](#0-5)

### Citations

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L612-623)
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
```

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L642-648)
```rust
    let mint_result = mint_icp_operation
        .mint_icp_with_ledger(ledger.as_ref(), now_seconds)
        .await;
    let Err(mint_error) = mint_result else {
        // Happy case: the minting was successful so we can exit here.
        return Ok(());
    };
```

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L650-674)
```rust
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
```

**File:** rs/nns/governance/tla/Disburse_Maturity_Timer.tla (L62-68)
```text
            if(answer.response = Variant("Fail", UNIT)) {
                either {
                    neuron := [neuron EXCEPT ![neuron_id].maturity_disbursements_in_progress = << current_disbursement >> \o @ ];
                    locks := locks \ {neuron_id};
                } or {
                    skip
                }
```

**File:** rs/nns/governance/src/neuron_lock.rs (L43-60)
```rust
impl Drop for NeuronAsyncLock {
    fn drop(&mut self) {
        if self.retain {
            return;
        }
        // In the case of a panic, the state of the ledger account representing the neuron's stake
        // may be inconsistent with the internal state of governance.  In that case, we want to
        // prevent further operations with that neuron until the issue can be investigated and
        // resolved, which will require code changes.
        if ic_cdk::futures::is_recovering_from_trap() {
            return;
        }
        // The lock is released when the NeuronAsyncLock is dropped. This is done to ensure that the lock
        // is released even if the NeuronAsyncLock is not explicitly unlocked.
        self.governance.with_borrow_mut(|governance| {
            governance.unlock_neuron(self.neuron_id.id);
        });
    }
```

**File:** rs/sns/governance/src/governance.rs (L5037-5069)
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
```
