### Title
Permanent Neuron Lock with No Timeout in `in_flight_commands` Blocks All Neuron Operations Indefinitely — (`File: rs/nns/governance/src/governance/disburse_maturity.rs`, `rs/nns/governance/src/neuron_lock.rs`)

---

### Summary

The NNS Governance canister's `in_flight_commands` map acts as a per-neuron lock for async ledger operations. Unlike the SNS governance's `upgrade_periodic_task_lock` (which has a hard 600-second timeout), the NNS neuron lock has **no timeout and no automated expiry**. In the `try_finalize_maturity_disbursement` function, a specific double-failure path explicitly calls `neuron_lock.retain()`, permanently inserting a lock entry into `in_flight_commands` with no mechanism to ever clear it automatically. Additionally, both `LedgerUpdateLock` and `NeuronAsyncLock` skip the unlock on canister trap (`is_recovering_from_trap()`), leaving the neuron permanently locked. The only recovery path is a governance upgrade with custom reconciliation code — an indefinite lockout of the user's neuron.

---

### Finding Description

**Root cause 1 — Explicit permanent lock retention in `disburse_maturity`:**

In `rs/nns/governance/src/governance/disburse_maturity.rs`, `try_finalize_maturity_disbursement` follows this sequence:

1. Acquires `NeuronAsyncLock` (inserts into `in_flight_commands`)
2. Pops the maturity disbursement from the neuron (mutation performed)
3. Calls the ledger to mint ICP — **this can fail**
4. On mint failure, attempts to reverse the neuron mutation (push the disbursement back)
5. If the reversal **also** fails, the code explicitly calls `neuron_lock.retain()` and returns an error [1](#0-0) 

The `retain()` call sets `self.retain = true` on the `NeuronAsyncLock`, causing its `Drop` implementation to skip the `unlock_neuron()` call: [2](#0-1) 

The lock entry remains in `in_flight_commands` indefinitely. The proto comment explicitly acknowledges this: *"If something goes fundamentally wrong (say we trap at some point after issuing a transfer call) the neuron(s) involved are left in a 'locked' state, meaning new operations can't be applied without reconciling the state."* [3](#0-2) 

**Root cause 2 — Trap-based permanent lock with no timeout:**

Both `LedgerUpdateLock` and `NeuronAsyncLock` skip the unlock if the canister is recovering from a trap: [4](#0-3) 

There is **no timeout** on entries in `in_flight_commands`. The NNS governance has no equivalent of the SNS governance's `UPGRADE_PERIODIC_TASK_LOCK_TIMEOUT_SECONDS = 600` that would auto-expire stale locks: [5](#0-4) 

The `get_delay_until_next_finalization` function even acknowledges that a locked neuron can be locked **indefinitely**: [6](#0-5) 

**Root cause 3 — No user-accessible recovery path:**

Once a neuron is locked in `in_flight_commands`, every subsequent `manage_neuron` call (disburse, split, merge, configure, spawn) returns `LedgerUpdateOngoing` error: [7](#0-6) 

There is no public canister method to clear a stale lock. The only documented recovery is "custom code added on upgrade, if necessary" — requiring a governance proposal and canister upgrade.

---

### Impact Explanation

A neuron holder whose neuron enters the retained-lock state loses the ability to:
- Disburse their ICP stake
- Split the neuron
- Merge neurons
- Configure dissolve delay
- Spawn maturity

The ICP stake is effectively frozen indefinitely. The `maturity_disbursements_in_progress` queue is also corrupted (disbursement was popped but not re-inserted), meaning the pending maturity disbursement is silently lost. The user's ICP governance tokens are locked with no automated recovery path, requiring a governance upgrade to fix — a process that takes days and requires community approval.

---

### Likelihood Explanation

The `FailToRestoreMaturityDisbursement` path requires two sequential failures: ledger minting failure followed by neuron mutation reversal failure. The reversal (`push_front_maturity_disbursement_in_progress`) would fail if the neuron is not found — which the code itself calls "impossible" but is a real code path. More practically, the trap-based permanent lock (Root cause 2) is reachable whenever the governance canister traps during any async ledger operation (e.g., due to instruction limit exhaustion, memory pressure, or a bug). Any neuron holder who calls `manage_neuron` with `Disburse`, `Split`, `Merge`, or `DisburseMaturity` during such a trap event will have their neuron permanently locked. Likelihood is **low-to-medium** for the double-failure path, and **low** but non-zero for the trap path, but the impact when triggered is severe and permanent.

---

### Recommendation

1. **Add a timeout to `in_flight_commands` entries** in NNS governance, analogous to `UPGRADE_PERIODIC_TASK_LOCK_TIMEOUT_SECONDS` in SNS governance. Stale locks older than a configurable threshold (e.g., 1 hour) should be automatically cleared by the periodic task.
2. **Add a public admin/governance method** to clear a specific stale lock entry from `in_flight_commands`, gated behind a governance proposal, to avoid requiring a full canister upgrade for recovery.
3. **In `try_finalize_maturity_disbursement`**, instead of retaining the lock permanently on `FailToRestoreMaturityDisbursement`, log the inconsistency and schedule a retry with exponential backoff, or emit a stable-memory alert that can be acted on without a full upgrade.
4. **Document the lock retention behavior** prominently in the canister's public interface so neuron holders are aware of the risk.

---

### Proof of Concept

**Triggering the `FailToRestoreMaturityDisbursement` path:**

1. User calls `manage_neuron` → `DisburseMaturity { percentage_to_disburse: 100, to_account: None }` on their neuron.
2. After `DISBURSEMENT_DELAY_SECONDS` (7 days), `maybe_finalize_disburse_maturity` runs via the periodic task.
3. `try_finalize_maturity_disbursement` is called:
   - Step 2: `pop_maturity_disbursement_in_progress()` succeeds — disbursement is removed from the neuron.
   - Step 3: `mint_icp_with_ledger()` fails (e.g., ledger temporarily unavailable).
   - Reversal: `push_front_maturity_disbursement_in_progress()` fails (e.g., neuron was concurrently modified or a bug causes `with_neuron_mut` to return `Err`).
4. `neuron_lock.retain()` is called at line 668.
5. The `NeuronAsyncLock` drops with `retain = true`, skipping `unlock_neuron()`.
6. `in_flight_commands` now permanently contains the neuron's ID.
7. All subsequent `manage_neuron` calls for this neuron return `LedgerUpdateOngoing` error indefinitely.
8. The maturity disbursement is also silently lost (not re-queued). [8](#0-7)

### Citations

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

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L697-701)
```rust
    if is_neuron_locked {
        // The first neuron eligible for finalization is locked. We should not ignore it since it
        // can be unlocked any time, but we also don't want to retry immediately as it can be locked
        // indefinitely. Therefore, we try to execute at the scheduled time but with throttling.
        delay_until_next_finalization.min(RETRY_INTERVAL)
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

**File:** rs/nns/governance/src/neuron_lock.rs (L79-99)
```rust
impl Drop for LedgerUpdateLock {
    fn drop(&mut self) {
        if self.retain {
            return;
        }
        // In the case of a panic, the state of the ledger account representing the neuron's stake
        // may be inconsistent with the internal state of governance.  In that case,
        // we want to prevent further operations with that neuron until the issue can be
        // investigated and resolved, which will require code changes.
        if ic_cdk::futures::is_recovering_from_trap() {
            return;
        }
        // It's always ok to dereference the governance when a LedgerUpdateLock
        // goes out of scope. Indeed, in the scope of any Governance method,
        // &self always remains alive. The 'mut' is not an issue, because
        // 'unlock_neuron' will verify that the lock exists.
        //
        // See "Recommendations for Using `unsafe` in the Governance canister" in canister.rs
        let gov: &mut Governance = unsafe { &mut *self.gov };
        gov.unlock_neuron(self.nid);
    }
```

**File:** rs/nns/governance/src/neuron_lock.rs (L219-238)
```rust
    pub(crate) fn lock_neuron_for_command(
        &mut self,
        id: u64,
        command: NeuronInFlightCommand,
    ) -> Result<LedgerUpdateLock, GovernanceError> {
        if self.heap_data.in_flight_commands.contains_key(&id) {
            return Err(GovernanceError::new_with_message(
                ErrorType::LedgerUpdateOngoing,
                "Neuron has an ongoing ledger update.",
            ));
        }

        self.heap_data.in_flight_commands.insert(id, command);

        Ok(LedgerUpdateLock {
            nid: id,
            gov: self,
            retain: false,
        })
    }
```

**File:** rs/nns/governance/proto/ic_nns_governance/pb/v1/governance.proto (L2165-2175)
```text
  // If there are no ongoing requests, this map should be empty.
  //
  // If something goes fundamentally wrong (say we trap at some point
  // after issuing a transfer call) the neuron(s) involved are left in a
  // "locked" state, meaning new operations can't be applied without
  // reconciling the state.
  //
  // Because we know exactly what was going on, we should have the
  // information necessary to reconcile the state, using custom code
  // added on upgrade, if necessary.
  map<fixed64, NeuronInFlightCommand> in_flight_commands = 10;
```

**File:** rs/sns/governance/src/governance.rs (L183-185)
```rust
/// The maximum duration for which the upgrade periodic task lock may be held.
/// Past this duration, the lock will be automatically released.
pub const UPGRADE_PERIODIC_TASK_LOCK_TIMEOUT_SECONDS: u64 = 600;
```
