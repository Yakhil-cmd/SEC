### Title
Permanently Retained Neuron Lock With No Runtime Emergency Unlock Mechanism - (`File: rs/nns/governance/src/governance/disburse_maturity.rs`, `rs/nns/governance/src/governance.rs`)

### Summary

NNS Governance contains two code paths where a neuron's `in_flight_commands` lock is explicitly retained forever (`neuron_lock.retain()`) after a double-failure scenario. No runtime function exists to clear a stuck lock entry; the only resolution is a canister upgrade with custom reconciliation code. This is a structural analog to the original report's "no emergency invalidation mechanism" class: a locked resource with no protocol-level escape hatch.

---

### Finding Description

**Path 1 — `try_finalize_maturity_disbursement`**

In `rs/nns/governance/src/governance/disburse_maturity.rs`, the async function `try_finalize_maturity_disbursement` follows this sequence:

1. Acquires a `NeuronAsyncLock` on the neuron via `Governance::acquire_neuron_async_lock`.
2. Pops the pending maturity disbursement from the neuron (first mutation).
3. Calls the ICP ledger to mint ICP.
4. If the mint fails, attempts to push the disbursement back onto the neuron (reversal).
5. If the reversal **also** fails (e.g., neuron not found in store), calls `neuron_lock.retain()` and returns an error. [1](#0-0) 

The `NeuronAsyncLock::retain()` flag prevents the lock from being released on drop: [2](#0-1) 

**Path 2 — `spawn_neurons`**

In `rs/nns/governance/src/governance.rs`, the `spawn_neurons` heartbeat task follows a similar pattern: it mutates the neuron's stake fields, calls the ledger, and if the ledger fails AND the state reversion fails (neuron not found), it calls `lock.retain()`: [3](#0-2) 

**No runtime unlock function exists**

The `in_flight_commands` map in NNS Governance proto is the backing store for all neuron locks: [4](#0-3) 

The proto comment explicitly acknowledges the stuck-lock scenario and states the only resolution is "custom code added on upgrade, if necessary." There is no `fail_stuck_neuron_lock` or equivalent runtime endpoint in NNS Governance (unlike SNS Governance, which has `fail_stuck_upgrade_in_progress`): [5](#0-4) 

NNS Governance has no analogous function.

---

### Impact Explanation

When a neuron's lock is retained, the neuron's entry persists in `in_flight_commands`. Any subsequent call to `lock_neuron_for_command` or `acquire_neuron_async_lock` for that neuron ID returns `ErrorType::LedgerUpdateOngoing`: [6](#0-5) 

The neuron owner permanently loses the ability to:
- Disburse stake
- Split the neuron
- Spawn a child neuron
- Merge neurons
- Perform any other stake-changing operation

The neuron's ICP stake is effectively frozen with no user-accessible recovery path. The only resolution requires an NNS governance proposal to upgrade the canister with custom state-clearing code — a process that takes days and requires a governance majority.

---

### Likelihood Explanation

The double-failure trigger requires:
1. The ICP ledger mint/transfer call to fail (transient or permanent ledger error).
2. The subsequent neuron mutation reversal to also fail — specifically, `with_neuron_mut` returning an error because the neuron is not found in the store.

The code comments describe condition 2 as "should be impossible." However, the ckBTC minter upgrade notes demonstrate that "should be impossible" stuck states do occur in production on the IC (the June 2025 and March 2026 emergency upgrades were specifically to unblock stuck withdrawal transactions): [7](#0-6) [8](#0-7) 

Likelihood is **low but non-zero**: it requires a specific combination of ledger failure and a concurrent neuron-store inconsistency. The absence of a runtime escape hatch means that when it does occur, the impact is permanent until an emergency upgrade is deployed.

---

### Recommendation

Add a `fail_stuck_neuron_lock(neuron_id: NeuronId)` function to NNS Governance (analogous to SNS Governance's `fail_stuck_upgrade_in_progress`) that:
- Is callable only by the governance canister itself (via proposal execution) or by a designated admin principal.
- Removes the specified entry from `in_flight_commands` after verifying the lock timestamp is older than a safe threshold (e.g., 24 hours).
- Emits an audit log entry recording the forced unlock.

This mirrors the pattern already established in SNS Governance: [9](#0-8) 

---

### Proof of Concept

**Trigger sequence for Path 1:**

1. Neuron controller calls `manage_neuron` → `DisburseMaturity` on a neuron with a pending disbursement scheduled for finalization.
2. The governance timer fires `try_finalize_maturity_disbursement`.
3. The neuron lock is acquired; the disbursement is popped from the neuron (mutation committed to stable store).
4. The ICP ledger `transfer_funds` call fails (e.g., ledger canister is temporarily stopped or returns an error).
5. The reversal call `neuron.push_front_maturity_disbursement_in_progress(...)` fails because the neuron was concurrently removed from the store (e.g., due to a separate bug or race).
6. `neuron_lock.retain()` is called at line 668 of `disburse_maturity.rs`.
7. The neuron's entry in `in_flight_commands` persists across canister upgrades (it is stored in stable proto state).
8. All subsequent `manage_neuron` calls for this neuron return `ErrorType::LedgerUpdateOngoing` indefinitely.
9. No user-callable function exists to clear the lock. The neuron owner's ICP stake is permanently inaccessible without an NNS upgrade proposal. [10](#0-9)

### Citations

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L557-675)
```rust
/// Returns an error if there is anything unexpected.
async fn try_finalize_maturity_disbursement(
    governance: &'static LocalKey<RefCell<Governance>>,
) -> Result<(), FinalizeMaturityDisbursementError> {
    let (maturity_disbursement_finalization, now_seconds) = governance.with_borrow(|governance| {
        let now_seconds = governance.env.now();
        let maturity_modulation = governance
            .heap_data
            .maturity_modulation
            .as_ref()
            .and_then(|m| m.current_value_permyriad);
        let maturity_disbursement_finalization = next_maturity_disbursement_to_finalize(
            &governance.neuron_store,
            &governance.heap_data.in_flight_commands,
            maturity_modulation,
            now_seconds,
        );
        (maturity_disbursement_finalization, now_seconds)
    });

    let Some(MaturityDisbursementFinalization {
        neuron_id,
        destination,
        amount_to_mint_e8s,
        original_maturity_e8s_equivalent,
        finalize_disbursement_timestamp_seconds,
    }) = maturity_disbursement_finalization?
    else {
        // No disbursement to finalize.
        return Ok(());
    };

    // Step 1: acquire a lock on the neuron, before any mutation is performed. Note that there
    // should not be any `await` before this point, otherwise any data accessed at this point can be
    // stale. Unfortunately we cannot acquire the lock sooner, since the content of the lock needs
    // to be computed above.
    let Ok(mut neuron_lock) = Governance::acquire_neuron_async_lock(
        governance,
        neuron_id,
        now_seconds,
        Command::FinalizeDisburseMaturity(FinalizeDisburseMaturity {
            amount_to_mint_e8s,
            to_account: destination.into_account(),
            to_account_identifier: destination.into_account_identifier_proto(),
            finalize_disbursement_timestamp_seconds,
            original_maturity_e8s_equivalent,
        }),
    ) else {
        // This should be impossible since we just checked the neuron is not locked when finding the
        // neuron.
        return Err(FinalizeMaturityDisbursementError::FailToAcquireNeuronLock(
            neuron_id,
        ));
    };

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

**File:** rs/nns/governance/src/governance.rs (L6561-6572)
```rust
                                Err(e) => {
                                    println!(
                                        "{} Error reverting state for neuron: {:?}. Retaining lock: {}",
                                        LOG_PREFIX, neuron_id, e
                                    );
                                    // Retain the neuron lock, the neuron won't be able to undergo stake changing
                                    // operations until this is fixed.
                                    // This is different from what we do in most places because we usually rely
                                    // on trapping to retain the lock, but we can't do that here since we're not
                                    // working on a single neuron.
                                    lock.retain();
                                }
```

**File:** rs/nns/governance/proto/ic_nns_governance/pb/v1/governance.proto (L2155-2175)
```text
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

**File:** rs/sns/governance/canister/canister.rs (L526-535)
```rust
/// Marks an in progress upgrade that has passed its deadline as failed.
#[update]
fn fail_stuck_upgrade_in_progress(
    request: FailStuckUpgradeInProgressRequest,
) -> FailStuckUpgradeInProgressResponse {
    log!(INFO, "fail_stuck_upgrade_in_progress");
    FailStuckUpgradeInProgressResponse::from(governance_mut().fail_stuck_upgrade_in_progress(
        sns_gov_pb::FailStuckUpgradeInProgressRequest::from(request),
    ))
}
```

**File:** rs/bitcoin/ckbtc/mainnet/minter_upgrade_2025_06_27.md (L17-33)
```markdown
## Motivation

Upgrade the ckBTC minter to try to unblock three transactions ckBTC → BTC (withdrawals) that are currently stuck since
2025.06.21.

After analysis, see this
forum [**post**](https://forum.dfinity.org/t/ckbtc-a-canister-issued-bitcoin-twin-token-on-the-ic-1-1-backed-by-btc/17606/202)
for more details, the problem appears to be due to the following:

1. An extremely low fee per vbyte was chosen by the minter for those transactions, which prevented them from being mined
   in the first place. We currently don’t have a satisfying explanation for how this low median fee was computed and are
   also investigating the bitcoin canister. A stop-gap solution was introduced
   in [#5742](https://github.com/dfinity/ic/pull/5742), to ensure that the fee per vbyte computed by the minter is
   always at least 1.5 sats/vbyte (for Bitcoin Mainnet).
2. There is a deterministic panic occurring in the minter when it tries to resubmit those transactions, which explains
   why those transactions are currently stuck. This should be completely fixed
   by [#5713](https://github.com/dfinity/ic/pull/5713).
```

**File:** rs/bitcoin/ckbtc/mainnet/minter_upgrade_2026_03_20.md (L17-28)
```markdown
## Motivation

Due to the security incident explained in this [forum post](https://forum.dfinity.org/t/proposal-140929-to-upgrade-the-ckbtc-minter/65401/3), the following ckBTC withdrawals (ckBTC -> BTC) are currently stuck:

* [3459007](https://dashboard.internetcomputer.org/bitcoin/transaction/3459007), [3459009](https://dashboard.internetcomputer.org/bitcoin/transaction/3459009), and [3459013](https://dashboard.internetcomputer.org/bitcoin/transaction/3459013) because the transaction from the minter tries to reuse the already spent output [`91bb46443799335076fbcd117f2295c7499d02dd3a59c22a531d31591114b303:5`](https://mempool.space/tx/91bb46443799335076fbcd117f2295c7499d02dd3a59c22a531d31591114b303#vout=5).
* [3489347](https://dashboard.internetcomputer.org/bitcoin/transaction/3489347) and [3489353](https://dashboard.internetcomputer.org/bitcoin/transaction/3489353) because the transaction from the minter tries to reuse the already spent output [`8942e5ef0d4ace158a4fddd5153d320701bd13370ff8fecef13795cdd8ff1dc5:1`](https://mempool.space/tx/8942e5ef0d4ace158a4fddd5153d320701bd13370ff8fecef13795cdd8ff1dc5#vout=1).

This proposal should address these issues by:
* Removing the duplicate outpoints from the minter's state.
* Discarding any transaction sent by the minter to the Bitcoin network that uses one of the duplicate outpoints. This is safe to do because those transactions are invalid and will never be accepted by the Bitcoin network.

The expected result is that the aforementioned withdrawals are considered as pending by the minter, as if they were going to be processed by the minter for the first time.
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
