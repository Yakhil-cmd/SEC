### Title
NNS Governance Neuron Lock (`in_flight_commands`) Has No Expiration Mechanism — (`File: rs/nns/governance/src/neuron_lock.rs`)

---

### Summary

The NNS Governance canister uses an `in_flight_commands` map to lock neurons during async ledger operations. When a trap occurs during such an operation, the lock is intentionally retained with no automatic expiration. There is no periodic cleanup or timeout for stale entries. A neuron that becomes permanently locked cannot be operated on by its owner without a canister upgrade containing custom reconciliation code.

---

### Finding Description

The `in_flight_commands` map in NNS Governance stores `NeuronInFlightCommand` entries keyed by neuron ID. These entries act as exclusive locks preventing concurrent neuron operations. Two lock types exist: `LedgerUpdateLock` and `NeuronAsyncLock`.

Both lock types explicitly check `ic_cdk::futures::is_recovering_from_trap()` in their `Drop` implementations and skip the unlock if a trap is detected: [1](#0-0) [2](#0-1) 

Additionally, the `retain()` method can be called explicitly to permanently hold the lock even on normal drop: [3](#0-2) 

This is used in production code paths such as `disburse_maturity.rs`: [4](#0-3) 

The `in_flight_commands` map itself has no TTL, no periodic sweep, and no expiration field on `NeuronInFlightCommand`: [5](#0-4) 

The proto comment explicitly acknowledges the stuck-lock scenario and defers recovery to "custom code added on upgrade, if necessary": [6](#0-5) 

The same pattern exists in SNS Governance: [7](#0-6) 

---

### Impact Explanation

A neuron whose lock entry is stuck in `in_flight_commands` is permanently frozen. The lock check in `lock_neuron_for_command` returns `LedgerUpdateOngoing` for any subsequent operation: [8](#0-7) 

The neuron owner cannot disburse, split, merge, spawn, vote, or perform any other operation. The only recovery path is a governance canister upgrade with bespoke reconciliation code — an out-of-band privileged action. Unlike the SNS `pending_version` which has `mark_failed_at_seconds` and `fail_stuck_upgrade_in_progress`: [9](#0-8) 

…the NNS `in_flight_commands` has no equivalent self-healing mechanism.

---

### Likelihood Explanation

The trigger condition — a trap during an async ledger callback — can occur due to: ledger canister panics, message timeouts, or explicit `retain()` calls in error paths. The `retain()` call in `disburse_maturity.rs` is a production code path that fires when a ledger mint fails and the neuron state cannot be reversed. Any user who initiates a `DisburseMaturity` operation under adverse ledger conditions can end up with a permanently locked neuron. The likelihood is low under normal conditions but non-negligible under ledger stress or bugs.

---

### Recommendation

Add a `timestamp` field (already present in `NeuronInFlightCommand`) to drive a periodic sweep in `run_periodic_tasks` that clears locks older than a configurable threshold (e.g., 24 hours), analogous to the SNS `mark_failed_at_seconds` / `fail_stuck_upgrade_in_progress` pattern. Alternatively, expose a governance-controlled `clear_neuron_lock` method gated on lock age.

---

### Proof of Concept

1. User calls `disburse_maturity` on their neuron.
2. Governance acquires a `NeuronAsyncLock` / `LedgerUpdateLock` and inserts into `in_flight_commands`.
3. The ledger mint call fails; `reverse_neuron_result` also fails.
4. `neuron_lock.retain()` is called — lock is marked to survive drop.
5. The lock entry remains in `in_flight_commands` indefinitely.
6. All subsequent `manage_neuron` calls for that neuron return `ErrorType::LedgerUpdateOngoing`.
7. No periodic task ever removes the entry; no expiration field exists.
8. Recovery requires a canister upgrade with custom reconciliation code. [10](#0-9) [11](#0-10)

### Citations

**File:** rs/nns/governance/src/neuron_lock.rs (L43-61)
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

**File:** rs/nns/governance/src/neuron_lock.rs (L102-106)
```rust
impl LedgerUpdateLock {
    pub(crate) fn retain(&mut self) {
        self.retain = true;
    }
}
```

**File:** rs/nns/governance/src/neuron_lock.rs (L156-179)
```rust
        let lock_acquired = governance.with_borrow_mut(|governance| {
            match governance.heap_data.in_flight_commands.entry(neuron_id.id) {
                Entry::Occupied(_) => false,
                Entry::Vacant(entry) => {
                    entry.insert(NeuronInFlightCommand {
                        command: Some(command),
                        timestamp,
                    });
                    true
                }
            }
        });
        if lock_acquired {
            Ok(NeuronAsyncLock {
                neuron_id,
                governance,
                retain: false,
            })
        } else {
            Err(GovernanceError::new_with_message(
                ErrorType::LedgerUpdateOngoing,
                "Neuron has an ongoing ledger update.",
            ))
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

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L657-675)
```rust
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

**File:** rs/nns/governance/proto/ic_nns_governance/pb/v1/governance.proto (L2119-2175)
```text
  // The possible commands that require interaction with the ledger.
  message NeuronInFlightCommand {
    // The timestamp at which the command was issued, for debugging
    // purposes.
    uint64 timestamp = 1;
    reserved 6;
    reserved "claim_or_refresh";
    reserved 4;

    // A general place holder for sync commands. The neuron lock is
    // never left holding a sync command (as it either succeeds to
    // acquire the lock and releases it in the same call, or never
    // acquires it in the first place), but it still must be acquired
    // to prevent interleaving with another async command. Thus there's
    // no value in actually storing the command itself, and this placeholder
    // can generally be used in all sync cases.
    message SyncCommand {}

    oneof command {
      ManageNeuron.Disburse disburse = 2;
      ManageNeuron.Split split = 3;
      ManageNeuron.DisburseToNeuron disburse_to_neuron = 5;
      ManageNeuron.MergeMaturity merge_maturity = 7;
      ManageNeuron.ClaimOrRefresh claim_or_refresh_neuron = 8;
      ManageNeuron.Configure configure = 9;
      ManageNeuron.Merge merge = 10;

      // Below are not really `ManageNeuron` commands but determined by the context of where the
      // neuron lock is needed. Ideally, we'd like to rename from `command` to `lock`
      ic_nns_common.pb.v1.NeuronId spawn = 20;
      SyncCommand sync_command = 21;
      FinalizeDisburseMaturity finalize_disburse_maturity = 22;
      CreateNeuron create_neuron = 23;
    }
  }

  // Set of in-flight neuron ledger commands.
  //
  // Whenever we issue a ledger transfer (for disburse, split, spawn etc)
  // we store it in this map, keyed by the id of the neuron being changed
  // and remove the entry when it completes.
  //
  // An entry being present in this map acts like a "lock" on the neuron
  // and thus prevents concurrent changes that might happen due to the
  // interleaving of user requests and callback execution.
  //
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

**File:** rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto (L1471-1492)
```text
  // The in-flight neuron ledger commands as a map from neuron IDs
  // to commands.
  //
  // Whenever we change a neuron in a way that must not interleave
  // with another neuron change, we store the neuron and the issued
  // command in this map and remove it when the command is complete.
  //
  // An entry being present in this map acts like a "lock" on the neuron
  // and thus prevents concurrent changes that might happen due to the
  // interleaving of user requests and callback execution.
  //
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
  map<string, NeuronInFlightCommand> in_flight_commands = 10;
```

**File:** rs/sns/governance/src/governance.rs (L6327-6361)
```rust
    /// Fails an upgrade proposal that was Adopted but not Executed or Failed by the deadline.
    pub fn fail_stuck_upgrade_in_progress(
        &mut self,
        _: FailStuckUpgradeInProgressRequest,
    ) -> FailStuckUpgradeInProgressResponse {
        let pending_version = match self.proto.pending_version.as_ref() {
            None => return FailStuckUpgradeInProgressResponse {},
            Some(pending_version) => pending_version,
        };

        // Maybe, we should look at the checking_upgrade_lock field and only
        // proceed if it is false, or the request has force set to true.

        let now = self.env.now();

        if now > pending_version.mark_failed_at_seconds {
            let message = format!(
                "Upgrade marked as failed at {}. \
                Governance upgrade was manually aborted by calling fail_stuck_upgrade_in_progress \
                after mark_failed_at_seconds ({}). Setting upgrade to failed to unblock retry.",
                format_timestamp_for_humans(now),
                pending_version.mark_failed_at_seconds,
            );
            let status = upgrade_journal_entry::upgrade_outcome::Status::ExternalFailure(Empty {});

            self.complete_sns_upgrade_to_next_version(
                pending_version.proposal_id,
                status,
                message,
                None,
            );
        }

        FailStuckUpgradeInProgressResponse {}
    }
```
