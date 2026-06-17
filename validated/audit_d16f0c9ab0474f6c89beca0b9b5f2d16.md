### Title
Upgrade Transaction Revert Causes Entire Block Halt via Unrecoverable Internal Error - (`basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs`)

---

### Summary

When an upgrade transaction (type `0x7E`) reverts during execution, the bootloader unconditionally raises an internal error that halts the entire block. An unprivileged L2 user who can observe the pending upgrade transaction in the L1 priority queue can front-run it to modify L2 state that the upgrade target contract depends on, causing the upgrade tx to revert and permanently halting the chain until the issue is manually resolved.

---

### Finding Description

In `process_l1_transaction`, after the upgrade transaction body executes, the following check is performed:

```rust
let to_refund_recipient = if !is_success {
    // Upgrade transactions must always succeed
    if !is_priority_op {
        return Err(internal_error!("Upgrade transaction must succeed").into());
    }
``` [1](#0-0) 

When `is_priority_op = false` (upgrade tx) and `is_success = false` (execution reverted), the function returns a `BootloaderSubsystemError` (internal error). This propagates via the `?` operator in `process_transaction`:

```rust
} else if transaction.is_upgrade() {
    if !is_first_tx {
        Err(TxError::Validation(InvalidTransaction::UpgradeTxNotFirst))
    } else {
        let r = F::process_l1_transaction::<Config>(...)?;
        Ok(r)
    }
``` [2](#0-1) 

The `?` converts the `BootloaderSubsystemError` into `TxError::Internal`. In the ZK tx loop, any `TxError::Internal` immediately halts the entire block:

```rust
Err(TxError::Internal(err)) => {
    system_log!(system, "Tx execution result: Internal error = {err:?}\n",);
    // Finish the frame opened before processing the tx
    system.finish_global_frame(None)?;
    return Err(err);
}
``` [3](#0-2) 

Unlike validation errors (which roll back the single transaction and continue), this internal error path does **not** roll back to `pre_tx_rollback_handle` — it calls `finish_global_frame(None)` (committing partial state) and then returns the error, aborting the entire block.

The upgrade tx is always the first transaction in a block: [4](#0-3) 

This behavior is confirmed by the existing test: [5](#0-4) 

**Attack path:**

1. Governance submits an upgrade tx on L1 targeting a contract on L2 whose logic depends on some L2 state (e.g., an initializable contract, a balance check, a mapping entry).
2. The upgrade tx is visible in the L1 priority queue before it is included in an L2 block.
3. An unprivileged attacker observes the pending upgrade tx and submits an L2 transaction that modifies the relevant L2 state (e.g., calls `initialize()` on the target contract first, or modifies a balance/mapping the upgrade logic checks).
4. The attacker's L2 tx is included in a block before the upgrade tx.
5. When the upgrade tx executes, the target contract reverts because the state has been modified.
6. The bootloader returns `internal_error!("Upgrade transaction must succeed")`.
7. The block fails entirely. The chain halts.

The documentation explicitly acknowledges the chain-halt risk for L1 transactions: [6](#0-5) 

However, the resilience measures applied to priority L1 txs (saturating arithmetic, graceful degradation) are **not** applied to upgrade txs — a reverting upgrade tx is treated as an unrecoverable internal error rather than a graceful failure.

---

### Impact Explanation

**Chain halt.** The sequencer cannot seal the block containing the upgrade transaction. Since upgrade txs come from the L1 priority queue and cannot be skipped, the chain stops processing all transactions until the issue is resolved out-of-band (e.g., by deploying a new upgrade tx that accounts for the modified state, or by rolling back the attacker's state change at the L1 level). This is a complete denial-of-service against the ZKsync OS chain.

Additionally, the `finish_global_frame(None)` call on the internal error path (line 111) commits the partial state from the failed upgrade tx's frame rather than rolling it back, potentially leaving the chain state in an inconsistent intermediate state. [3](#0-2) 

---

### Likelihood Explanation

**Medium.** The attack requires:
1. An upgrade tx whose target contract behavior depends on user-modifiable L2 state. This is realistic for complex upgrades (e.g., contract initialization, balance migrations, state-dependent logic).
2. An attacker who can observe the L1 priority queue and submit an L2 tx before the upgrade block is sealed. L1 transactions are publicly visible, making observation trivial.

Protocol upgrades are infrequent but high-value events. A motivated attacker (e.g., one who opposes a specific upgrade) has a clear incentive to execute this attack.

---

### Recommendation

1. **Treat upgrade tx reverts as a recoverable failure** rather than an internal error. Instead of `return Err(internal_error!("Upgrade transaction must succeed"))`, emit a system log, mark the upgrade as failed, and allow the block to continue (similar to how priority L1 tx validation errors are handled with saturating arithmetic).

2. **Roll back state on upgrade tx revert.** The current code calls `finish_global_frame(None)` on the internal error path, committing partial state. It should call `finish_global_frame(Some(&pre_tx_rollback_handle))` to ensure clean rollback.

3. **Design upgrade transactions to be idempotent** and not dependent on user-modifiable L2 state. Use checks like "if already initialized, skip" rather than "revert if already initialized."

---

### Proof of Concept

```
1. Deploy contract C on L2:
   function initialize() external {
       require(!initialized, "already initialized");
       initialized = true;
   }

2. Governance submits upgrade tx on L1:
   - target: contract C
   - calldata: initialize()
   - This tx enters the L1 priority queue and is visible publicly.

3. Attacker observes the pending upgrade tx and submits L2 tx:
   - call C.initialize()
   - This tx is included in a block before the upgrade block.

4. Upgrade block is assembled with the upgrade tx as the first tx.

5. Upgrade tx executes: C.initialize() reverts ("already initialized").

6. process_l1_transaction returns Err(internal_error!("Upgrade transaction must succeed")).

7. zk/tx_loop.rs line 112: return Err(err) — block halts.

8. Chain is halted. No further blocks can be sealed until the upgrade tx issue is resolved.
``` [1](#0-0) [7](#0-6)

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

**File:** tests/instances/transactions/src/l1_tx_resilience.rs (L1-11)
```rust
//!
//! Regression tests for L1 transaction processing resilience.
//!
//! These tests verify that L1 transactions are processed gracefully even when
//! certain validation constraints are violated. This is important because
//! L1 transactions cannot be invalidated (doing so would halt the chain due
//! to the priority queue).
//!
//! The scenarios tested here would have caused validation errors prior to the
//! resilience changes, but now use saturating arithmetic to allow processing
//! to continue.
```
