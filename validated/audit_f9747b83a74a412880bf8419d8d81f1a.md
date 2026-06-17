### Title
Block-Level Resource Cap Checks Can Invalidate L1 Priority Deposit Transactions - (`basic_bootloader/src/bootloader/block_flow/zk/tx_loop.rs`)

---

### Summary

`check_for_block_limits` in the ZK transaction loop applies block-level resource caps (gas, native, pubdata, logs, blob gas) uniformly to **all** transactions, including L1 priority deposit transactions. L1 deposits are supposed to be non-invalidatable by design, but the block-limit check can revert them at the block level, rolling back all state changes including the deposit mint and refund. An attacker who fills a block with resource-heavy L2 transactions can permanently prevent L1 deposits from being processed, locking user funds on L1 indefinitely.

---

### Finding Description

In `basic_bootloader/src/bootloader/block_flow/zk/tx_loop.rs`, after every transaction completes successfully, the loop checks whether the cumulative block resource usage has exceeded any limit: [1](#0-0) 

The check is applied unconditionally to the result of `process_transaction`, regardless of whether the transaction is an L1 priority deposit (`is_priority_tx`). If any limit is exceeded, the entire transaction is rolled back via `pre_tx_rollback_handle`: [2](#0-1) 

The `check_for_block_limits` function checks five independent resource dimensions: [3](#0-2) 

The `is_priority_tx` flag is only consulted **after** the block-limit check passes (to record the tx hash in the enforced accumulator): [4](#0-3) 

Meanwhile, the codebase explicitly acknowledges that L1 transactions must not be invalidatable: [5](#0-4) 

Every L1 priority transaction unconditionally emits an L1→L2 log: [6](#0-5) 

This log is emitted inside `process_l1_transaction` (inside `process_transaction`), so it is counted in `system.io.logs_len()` when `check_for_block_limits` reads it at line 146. If the block is already at `MAX_NUMBER_OF_LOGS - 1`, the L1 deposit's log pushes the count to `MAX_NUMBER_OF_LOGS`, triggering `BlockL2ToL1LogsLimitReached`, and the entire deposit is rolled back.

The same applies to the pubdata limit: every L1 deposit generates intrinsic pubdata (`L1_TX_INTRINSIC_PUBDATA`) plus storage diffs from token minting. If the block's pubdata budget is nearly exhausted by prior L2 transactions, the deposit's pubdata contribution triggers `BlockPubdataLimitReached`. [7](#0-6) 

---

### Impact Explanation

When `check_for_block_limits` invalidates an L1 priority deposit:

1. `system.finish_global_frame(Some(&pre_tx_rollback_handle))` reverts **all** state changes from the transaction, including the deposit mint, the operator fee payment, and the refund mint.
2. The user's funds remain locked on L1 with no corresponding L2 credit and no refund.
3. The priority queue on L1 still contains the transaction. The comment in the codebase notes this can halt the chain.

An attacker who fills every block with exactly `MAX_NUMBER_OF_LOGS - 1` logs (or pubdata up to the limit minus the L1 tx's intrinsic contribution) can permanently prevent any L1 deposit from being included, causing indefinite loss of access to bridged funds.

---

### Likelihood Explanation

The attacker needs to submit L2 transactions that consume block resources up to just below the limit in every block. This costs gas but is economically feasible as a griefing attack, especially if the attacker targets a specific victim's deposit. The L1 deposit's intrinsic log emission and intrinsic pubdata are fixed and predictable, making the threshold easy to target precisely.

---

### Recommendation

Before calling `check_for_block_limits`, check whether the transaction is an L1 priority transaction (`tx_processing_result.is_priority_tx`). L1 priority transactions must not be subject to block-level resource cap invalidation. If a block is full, the sequencer should seal the block and carry the L1 tx into the next block — but the proving path must never silently drop an L1 deposit by rolling it back without a refund.

Concretely, in `tx_loop.rs`, skip `check_for_block_limits` (or handle it as a block-seal signal rather than a transaction invalidation) when `tx_processing_result.is_priority_tx` is true.

---

### Proof of Concept

1. Attacker submits L2 transactions that each emit one L2→L1 log (e.g., via `LOG1` opcode), filling the block to `MAX_NUMBER_OF_LOGS - 1` logs.
2. A victim submits an L1 priority deposit transaction.
3. `process_l1_transaction` executes successfully and emits the mandatory L1→L2 tx log at line 365.
4. Back in `tx_loop.rs` line 146: `block_logs_used = system.io.logs_len()` = `MAX_NUMBER_OF_LOGS`.
5. `check_for_block_limits` at line 84 returns `Err(InvalidTransaction::BlockL2ToL1LogsLimitReached)`.
6. Line 161: `system.finish_global_frame(Some(&pre_tx_rollback_handle))` — all deposit state changes, including the token mint and refund, are reverted.
7. Line 162: `result_keeper.tx_processed(Err(BlockL2ToL1LogsLimitReached))` — deposit recorded as failed.
8. Victim's funds remain locked on L1. Attacker repeats in every subsequent block. [8](#0-7) [9](#0-8) [10](#0-9)

### Citations

**File:** basic_bootloader/src/bootloader/block_flow/zk/tx_loop.rs (L144-162)
```rust
                            let next_block_pubdata_used =
                                block_data.block_pubdata_used + tx_processing_result.pubdata_used;
                            let block_logs_used = system.io.logs_len();
                            let next_block_blob_gas_used =
                                block_data.block_blob_gas_used + tx_processing_result.blob_gas_used;

                            // Check if the transaction made the block reach any of the limits
                            // for gas, native, pubdata or logs.
                            if let Err(err) = check_for_block_limits(
                                system,
                                next_block_gas_used,
                                next_block_computational_native_used,
                                next_block_pubdata_used,
                                block_logs_used,
                                next_block_blob_gas_used,
                            ) {
                                // Revert to state before transaction
                                system.finish_global_frame(Some(&pre_tx_rollback_handle))?;
                                result_keeper.tx_processed(Err(err));
```

**File:** basic_bootloader/src/bootloader/block_flow/zk/tx_loop.rs (L201-205)
```rust
                                if tx_processing_result.is_priority_tx {
                                    block_data
                                        .enforced_transaction_hashes_accumulator
                                        .add_tx_hash(&tx_processing_result.tx_hash);
                                    batch_data.add_tx_hash(&tx_processing_result.tx_hash);
```

**File:** basic_bootloader/src/bootloader/block_flow/zk/mod.rs (L44-94)
```rust
fn check_for_block_limits<S: EthereumLikeTypes>(
    system: &mut System<S>,
    gas_used: u64,
    computational_native_used: u64,
    pubdata_used: u64,
    logs_used: u64,
    blob_gas_used: u64,
) -> Result<(), InvalidTransaction>
where
    S::IO: IOSubsystemExt,
    <S as SystemTypes>::Metadata: ZkSpecificPricingMetadata,
{
    if gas_used > system.get_gas_limit() {
        system_log!(
            system,
            "Block gas limit reached, invalidating transaction\n"
        );
        Err(InvalidTransaction::BlockGasLimitReached)
    } else if blob_gas_used > system.get_blob_gas_limit() {
        system_log!(
            system,
            "Block blob gas limit reached, invalidating transaction\n"
        );
        Err(InvalidTransaction::BlockBlobGasLimitReached)
    } else if !cfg!(feature = "resources_for_tester")
        && computational_native_used > MAX_NATIVE_COMPUTATIONAL
    {
        // ZKsync OS-specific resources are not checked for evm tester
        system_log!(
            system,
            "Block native limit reached, invalidating transaction\n"
        );
        Err(InvalidTransaction::BlockNativeLimitReached)
    } else if !cfg!(feature = "resources_for_tester") && pubdata_used > system.get_pubdata_limit() {
        // ZKsync OS-specific resources are not checked for evm tester
        system_log!(
            system,
            "Block pubdata limit reached, invalidating transaction\n"
        );
        Err(InvalidTransaction::BlockPubdataLimitReached)
    } else if !cfg!(feature = "resources_for_tester") && logs_used > MAX_NUMBER_OF_LOGS {
        // ZKsync OS-specific resources are not checked for evm tester
        system_log!(
            system,
            "Block logs limit reached, invalidating transaction\n"
        );
        Err(InvalidTransaction::BlockL2ToL1LogsLimitReached)
    } else {
        Ok(())
    }
}
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L93-98)
```rust
    let extra_pubdata_for_simulation = if Config::SIMULATION {
        ASSET_TRACKER_INTRINSIC_PUBDATA
    } else {
        0
    };
    let intrinsic_pubdata = L1_TX_INTRINSIC_PUBDATA + extra_pubdata_for_simulation;
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L100-105)
```rust
    // Compute resource and fee information, making sure we handle
    // all possible validation errors carefully.
    // L1 transactions cannot be invalidated. Therefore, the following
    // function makes sure L1 transactions are processable even when
    // some checks that should be performed by the L1 don't hold.
    let ResourceAndFeeInfo {
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L364-371)
```rust
    if is_priority_op {
        system.io.emit_l1_l2_tx_log(
            ExecutionEnvironmentType::NoEE,
            &mut inf_resources,
            tx_hash,
            is_success,
        )?;
    }
```

**File:** basic_bootloader/src/bootloader/errors.rs (L95-104)
```rust
    /// Transaction makes the block reach the gas limit
    BlockGasLimitReached,
    /// Transaction makes the block reach the blob gas limit
    BlockBlobGasLimitReached,
    /// Transaction makes the block reach the native resource limit
    BlockNativeLimitReached,
    /// Transaction makes the block reach the pubdata limit
    BlockPubdataLimitReached,
    /// Transaction makes the block reach the l2->l1 logs limit
    BlockL2ToL1LogsLimitReached,
```
