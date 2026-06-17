### Title
Upgrade Transaction Revert Causes Permanent Chain Halt — (`basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs`)

---

### Summary

When an upgrade transaction (type `0x7E`) reverts during execution, the bootloader unconditionally returns an `InternalError` rather than handling the failure gracefully. Because upgrade transactions are committed to the L1 priority queue and must be the first transaction in every block until processed, a single reverting upgrade transaction permanently halts the chain: every subsequent block attempt fails with the same internal error, no transactions can be processed, and user funds on L2 are locked.

---

### Finding Description

In `process_l1_transaction.rs`, after the upgrade transaction body executes and `is_success` is `false`, the following hard-coded guard fires:

```rust
// Upgrade transactions must always succeed
if !is_priority_op {
    return Err(internal_error!("Upgrade transaction must succeed").into());
}
``` [1](#0-0) 

`is_priority_op` is `false` for upgrade transactions (type `0x7E`) and `true` for ordinary L1→L2 transactions (type `0x7F`). So any revert of an upgrade transaction — whether caused by a bug in the upgrade target contract, an out-of-gas condition, or attacker-manipulated L2 state — unconditionally returns a `BootloaderSubsystemError`.

This error propagates back to `process_transaction`:

```rust
let r = F::process_l1_transaction::<Config>(
    system, system_functions, memories, zk_tx,
    false,   // is_priority_op = false for upgrade tx
    tracer, validator,
)?;
``` [2](#0-1) 

And is caught in the ZK transaction loop as a `TxError::Internal`, which immediately aborts the entire block:

```rust
Err(TxError::Internal(err)) => {
    system_log!(system, "Tx execution result: Internal error = {err:?}\n",);
    // Finish the frame opened before processing the tx
    system.finish_global_frame(None)?; // TODO should we use pre_tx_rollback_handle here?
    return Err(err);
}
``` [3](#0-2) 

Note the `TODO` comment: the frame is closed without the rollback handle, meaning any partial state mutations from the failed upgrade tx are **not** reverted at the tx-loop level (though the inner `rollback_handle` inside `process_l1_transaction` does revert the EVM-level state changes).

The upgrade transaction is validated to be the first transaction in a block:

```rust
if transaction.is_upgrade() {
    if !is_first_tx {
        Err(TxError::Validation(InvalidTransaction::UpgradeTxNotFirst))
    } else {
        let r = F::process_l1_transaction::<Config>(..., false, ...)?;
``` [4](#0-3) 

Because the upgrade tx is in the L1 priority queue and must be the first tx in every block, and because a revert causes an `InternalError` that aborts block execution, the chain cannot produce any valid block. There is no in-protocol recovery path within the bootloader.

This is confirmed by the existing test:

```rust
/// Test that upgrade transactions (L1 -> L2) that revert raise an internal error
/// instead of a validation error.
#[test]
fn test_upgrade_tx_revert_internal_error() {
    ...
    assert!(error_debug.contains("Upgrade transaction must succeed"), ...);
}
``` [5](#0-4) 

The test explicitly documents and asserts this behavior — the codebase treats it as expected, but the consequence (permanent chain halt) is not addressed.

---

### Impact Explanation

- **Chain halt**: every block execution attempt fails with `InternalError("Upgrade transaction must succeed")` as long as the reverting upgrade tx remains at the head of the L1 priority queue.
- **User funds locked**: no L2 transactions can be processed; withdrawals and transfers are frozen.
- **No in-protocol recovery**: the bootloader has no mechanism to skip, cancel, or bypass a stuck upgrade transaction. Recovery requires L1-level governance intervention to cancel the priority queue entry — if such a mechanism exists on the settlement layer.

---

### Likelihood Explanation

The trigger requires an upgrade transaction to be submitted to the L1 priority queue whose target contract reverts. This can happen via:

1. **Governance mistake**: the upgrade target contract contains a bug that causes it to revert (e.g., a storage precondition that is not met at execution time).
2. **Attacker-induced revert**: if the upgrade target contract reads L2 storage to gate its logic, an attacker can front-run the upgrade block by writing to that storage slot, causing the upgrade contract to revert. The attacker does not need any privileged role — only the ability to submit an L2 transaction before the upgrade block is sealed.

This is directly analogous to the LimboDAO finding: governance passes a proposal (submits an upgrade tx) that cannot execute, and the system has no recovery path. The LimboDAO judge noted the same pattern: "this attack vector requires the community to misbehave or at least be imprudent."

---

### Recommendation

1. **Treat upgrade transaction reverts as a validation error, not an internal error.** Return a `TxError::Validation` (or a dedicated `UpgradeTxReverted` variant) so the block can be sealed without the upgrade tx, and the sequencer can signal the failure without halting.
2. **Add a recovery path**: allow the sequencer to produce a block that marks the upgrade tx as failed and removes it from the priority queue, similar to how L1→L2 priority transactions handle reverts (refund the deposit, emit a failure log, continue).
3. **Fix the `finish_global_frame(None)` TODO** at `tx_loop.rs:111`: on internal error, use `finish_global_frame(Some(&pre_tx_rollback_handle))` to ensure all state mutations are cleanly reverted.

---

### Proof of Concept

1. Governance submits an upgrade transaction on L1 targeting contract `C` on L2.
2. An attacker observes the pending upgrade tx and submits an L2 transaction that writes to a storage slot that `C` reads as a precondition (e.g., a "paused" flag), causing `C` to revert.
3. The sequencer seals a block with the upgrade tx as the first transaction.
4. `execute_l1_transaction_and_notify_result` executes `C`, which reverts → `is_success = false`.
5. `process_l1_transaction` reaches line 314: `!is_priority_op` is `true` → returns `InternalError("Upgrade transaction must succeed")`.
6. `tx_loop` catches `TxError::Internal` → calls `finish_global_frame(None)` → returns `Err(err)`.
7. `BasicBootloader::run_prepared` propagates the error → block execution fails.
8. The upgrade tx remains at the head of the L1 priority queue.
9. Every subsequent block attempt repeats steps 3–8.
10. The chain is permanently halted; all L2 user funds are frozen. [1](#0-0) [6](#0-5) [4](#0-3)

### Citations

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L312-316)
```rust
    let to_refund_recipient = if !is_success {
        // Upgrade transactions must always succeed
        if !is_priority_op {
            return Err(internal_error!("Upgrade transaction must succeed").into());
        }
```

**File:** basic_bootloader/src/bootloader/transaction_flow/process_transaction.rs (L28-42)
```rust
                if transaction.is_upgrade() {
                    if !is_first_tx {
                        Err(TxError::Validation(InvalidTransaction::UpgradeTxNotFirst))
                    } else {
                        let r = F::process_l1_transaction::<Config>(
                            system,
                            system_functions,
                            memories,
                            zk_tx,
                            false,
                            tracer,
                            validator,
                        )?;
                        Ok(r)
                    }
```

**File:** basic_bootloader/src/bootloader/block_flow/zk/tx_loop.rs (L107-113)
```rust
                    match tx_result {
                        Err(TxError::Internal(err)) => {
                            system_log!(system, "Tx execution result: Internal error = {err:?}\n",);
                            // Finish the frame opened before processing the tx
                            system.finish_global_frame(None)?; // TODO should we use pre_tx_rollback_handle here?
                            return Err(err);
                        }
```

**File:** tests/instances/transactions/src/lib.rs (L834-870)
```rust
/// Test that upgrade transactions (L1 -> L2) that revert raise an internal error
/// instead of a validation error.
#[test]
fn test_upgrade_tx_revert_internal_error() {
    // Create a contract that always reverts
    let revert_contract_address = address!("0000000000000000000000000000000000010003");
    // Simple contract bytecode that just does REVERT(0, 0)
    let revert_bytecode = hex::decode("60006000fd").unwrap(); // PUSH1 0, PUSH1 0, REVERT
    let mut tester =
        TestingFramework::new().with_evm_contract(revert_contract_address, &revert_bytecode);

    // Create a proper upgrade transaction that calls the reverting contract

    let upgrade_tx = ZKsyncTxEnvelope::from(ZKsyncUpgradeTx {
        from: address!("1234000000000000000000000000000000000000"),
        to: revert_contract_address,
        gas_limit: 100_000u128,
        ..Default::default()
    });

    let transactions = vec![upgrade_tx];

    // Use execute_block_no_panic to catch the error instead of panicking
    let result = tester.execute_block_no_panic(transactions);

    // The upgrade transaction should fail with an internal error (not validation error)
    assert!(result.is_err());

    // The error should be an internal error containing "Upgrade transaction must succeed"
    let error = result.unwrap_err();
    let error_debug = format!("{:?}", error);
    assert!(
        error_debug.contains("Upgrade transaction must succeed"),
        "Expected error to contain 'Upgrade transaction must succeed', got: {}",
        error_debug
    );
}
```
