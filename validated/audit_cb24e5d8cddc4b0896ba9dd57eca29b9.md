Audit Report

## Title
Permanent Maturity Loss and Neuron Lock Retention on Double Failure in Finalization - (`rs/nns/governance/src/governance/disburse_maturity.rs`)

## Summary

In `try_finalize_maturity_disbursement`, if the ICP ledger minting call fails and the subsequent attempt to push the disbursement back onto the neuron's queue also fails, the code calls `neuron_lock.retain()` at line 668. This permanently retains the neuron's entry in `in_flight_commands`, freezing the neuron from all future maturity disbursement finalization. The maturity that was deducted from `neuron.maturity_e8s_equivalent` at initiation time is permanently lost — it is neither in the neuron nor minted to the user.

## Finding Description

**Initiation path** (`initiate_maturity_disbursement`, lines 317–326): When a user calls `DisburseMaturity`, the governance canister atomically deducts `disbursement_maturity_e8s` from `neuron.maturity_e8s_equivalent` and appends a `MaturityDisbursement` entry to the neuron's queue. The maturity is gone from the neuron's balance from this point forward.

**Finalization path** (`try_finalize_maturity_disbursement`, lines 558–675): After 7 days, the timer task calls this function. The critical sequence is:

1. **Step 2** (line 615–623): `pop_maturity_disbursement_in_progress()` removes the entry from the neuron's queue — mutation #1 committed.
2. **Step 3** (line 642–648): `mint_icp_with_ledger()` is called. If it returns `Err`, execution falls through to the reversal path.
3. **Reversal** (lines 652–663): `push_front_maturity_disbursement_in_progress()` is called via `with_neuron_mut`. If this also returns `Err` (e.g., the neuron is not found in stable storage, or stable memory write fails), execution falls through to the terminal path.
4. **Terminal path** (lines 665–674): `neuron_lock.retain()` is called, setting `retain = true` on the `NeuronAsyncLock`. The `Drop` implementation at `neuron_lock.rs` line 44–60 skips `unlock_neuron()` when `retain` is true, leaving the neuron's ID permanently in `heap_data.in_flight_commands`.

The `next_maturity_disbursement_to_finalize` function (lines 462–468) filters out any neuron whose ID is in `in_flight_commands`, so the neuron is permanently skipped by the finalization timer. The `get_delay_until_next_finalization` function (lines 692–701) does schedule retries for locked neurons, but since the lock is never released, the timer will loop indefinitely without making progress.

The `in_flight_commands` map is serialized in the governance proto (field 10) and persists across canister upgrades, so the lock survives restarts. The proto comment acknowledges this: "reconcile the state, using custom code added on upgrade, if necessary" — but no such code exists for this specific failure mode, and no user-callable or admin-callable method exists to clear the lock.

## Impact Explanation

This is a **permanent loss of NNS governance maturity** (ICP-equivalent rewards) for affected neurons, combined with a **permanent neuron lock** that prevents all future maturity disbursement finalization for the affected neuron. The maturity deducted at initiation is neither returned to the neuron nor minted to the user — it is destroyed. The neuron remains locked indefinitely, blocking all subsequent disbursements queued behind the stuck entry. This matches the allowed High impact: "Significant NNS security impact with concrete user or protocol harm" — permanent destruction of user governance rewards and permanent impairment of neuron functionality.

## Likelihood Explanation

The double-failure requires two independent conditions:

1. **Ledger mint failure**: The ICP ledger `mint_icp_with_ledger` call returns an error. This is realistic during ledger canister upgrades, transient inter-canister call failures, or cycles exhaustion on the governance canister.
2. **Reversal failure**: `with_neuron_mut` returns an error when attempting to push the disbursement back. This can occur if the neuron was deleted from stable storage between the pop and the push-back (e.g., via a concurrent governance upgrade that clears neurons), or if the stable memory `StableBTreeMap` write fails due to memory exhaustion. The `update` method in `StableNeuronStore` returns `NeuronStoreError::not_found` if the neuron's main entry is absent.

Each condition individually is low-probability but realistic. The combination is rare but not impossible, and the consequence is irreversible without a governance-approved canister upgrade containing custom recovery code.

## Recommendation

1. **Do not retain the lock on double failure.** At lines 665–674, instead of calling `neuron_lock.retain()`, release the lock normally and log a critical error. The maturity is already lost; retaining the lock compounds the damage by permanently freezing the neuron.
2. **Restore maturity on reversal failure.** If `push_front_maturity_disbursement_in_progress` fails, add `original_maturity_e8s_equivalent` back to `neuron.maturity_e8s_equivalent` directly via a separate `with_neuron_mut` call, so the user can re-initiate the disbursement.
3. **Emit a critical-level alert** (not just `println!`) when this path is reached, to ensure operators are notified immediately.

## Proof of Concept

**Minimal deterministic test plan** (PocketIC or integration test):

1. Create a neuron with sufficient maturity and call `DisburseMaturity`. Verify `maturity_e8s_equivalent` is reduced and a `MaturityDisbursement` entry is queued.
2. Advance time by 7+ days to make the disbursement eligible for finalization.
3. Configure a mock ledger that returns an error on `transfer`/`mint`.
4. Inject a mock neuron store that returns `NeuronStoreError::not_found` on the second `with_neuron_mut` call (the reversal call), while succeeding on the first (the pop call). This can be done by deleting the neuron from stable storage between the two calls in a test harness.
5. Trigger `finalize_maturity_disbursement`.
6. Assert: (a) `in_flight_commands` contains the neuron ID (lock retained), (b) `neuron.maturity_e8s_equivalent` does not include the disbursed amount, (c) the user's destination account received no ICP, (d) subsequent calls to `finalize_maturity_disbursement` skip the neuron indefinitely.

The TLA+ model at `rs/nns/governance/tla/Disburse_Maturity_Timer.tla` lines 62–71 explicitly models the double-failure branch (`skip` path) as a valid non-deterministic outcome, confirming the developers were aware of this state but did not implement a recovery mechanism for the maturity loss.