### Title
Fatal and Unrecoverable Block Halt via `L2AssetTracker.handleFinalizeBaseTokenBridgingOnL2` Revert During L1→L2 Transaction Processing - (`basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs`)

---

### Summary

The `notify_l2_asset_tracker` function in the ZKsync OS bootloader unconditionally treats any EVM revert from `L2AssetTracker.handleFinalizeBaseTokenBridgingOnL2` as a fatal `internal_error!` that propagates up through the transaction processing loop and permanently halts block execution. Because L1→L2 priority-queue transactions are processed in strict sequential order and cannot be skipped or reordered, a single L1→L2 deposit transaction that triggers a revert in `L2AssetTracker` can render the chain permanently unable to produce further blocks — an unrecoverable state with no on-chain recovery path.

---

### Finding Description

In `process_l1_transaction.rs`, the function `notify_l2_asset_tracker` is invoked up to three times per L1→L2 transaction that carries a non-zero deposit (`total_deposited > 0`): once for the value mint to the sender, once for the operator fee payment to coinbase, and once for the refund to the refund recipient. [1](#0-0) 

The function calls `L2AssetTracker.handleFinalizeBaseTokenBridgingOnL2(fromChainId, amount)` via `run_single_interaction`. If the EVM call returns a failure (revert), the function immediately returns a fatal `internal_error!`:

```rust
if failed {
    // A revert here means the chain's token accounting would be inconsistent.
    // Treated as a fatal system error — block processing cannot continue.
    return Err(internal_error!(
        "L2AssetTracker.handleFinalizeBaseTokenBridgingOnL2 reverted"
    ).into());
}
``` [2](#0-1) 

This `BootloaderSubsystemError` propagates up through `process_l1_transaction` → `process_transaction` → the ZK transaction loop in `tx_loop.rs`, where `TxError::Internal` causes an immediate `return Err(err)` — the loop terminates and block processing halts: [3](#0-2) 

The `L2AssetTracker` is called with two attacker-influenced parameters:
- `fromChainId` — read from `L2AssetTracker` storage slot 154 (fixed per deployment)
- `amount` — derived directly from the L1 transaction's `total_deposited`, `gas_price`, and `gas_limit` fields, all of which are set by the L1 transaction submitter [4](#0-3) 

The three call sites are:

1. **Operator fee mint** (always called when `pay_to_operator > 0`): [5](#0-4) 

2. **Refund recipient mint** (called when `to_refund_recipient > 0`): [6](#0-5) 

3. **Value mint to sender** (inside `execute_l1_transaction_and_notify_result`, called when `amount > 0`): [7](#0-6) 

The design comment explicitly acknowledges the halt-on-revert behavior:

> *"Failure halts block processing — if the asset tracker reverts, the chain's token accounting would be inconsistent, so we treat it as fatal rather than silently continuing with incorrect bookkeeping."* [8](#0-7) 

There is **no recovery mechanism**: no ability to skip the failing L1→L2 transaction, no fallback path, and no way to upgrade the bootloader without first processing the stuck priority queue entry (since upgrades are themselves L1→L2 transactions that must be processed in order).

The `prepare_and_check_resources` function explicitly documents that L1 transactions cannot be invalidated and uses saturating arithmetic to ensure they are always processable at the resource level: [9](#0-8) 

This makes the `notify_l2_asset_tracker` fatal-revert path the **only remaining way** a valid L1→L2 transaction can permanently halt the chain.

---

### Impact Explanation

If `L2AssetTracker.handleFinalizeBaseTokenBridgingOnL2` reverts for any reason during the processing of an L1→L2 deposit transaction, the ZKsync OS bootloader returns a fatal internal error. The transaction loop terminates immediately. No further transactions — L1 or L2 — can be processed. The chain is in an unrecoverable state: it cannot seal blocks, cannot process the priority queue, and cannot apply an upgrade to fix the issue (since upgrades are themselves L1→L2 transactions that would encounter the same stuck queue entry). This is a complete chain halt equivalent to the `BridgedGovernor` stuck-nonce scenario.

---

### Likelihood Explanation

Any user can submit an L1→L2 transaction with a non-zero `total_deposited` from L1 — this is the standard deposit flow. The `L2AssetTracker` is a real EVM contract called with user-influenced inputs (`amount` derived from `total_deposited`, `gas_price`, `gas_limit`). If the contract has any revert condition reachable via these inputs (e.g., arithmetic overflow in internal accounting, an uninitialized or unexpected state, a reentrancy guard, or a paused state), an attacker can craft a deposit transaction to trigger it. The `L2AssetTracker` is a complex upgradeable contract with interop accounting logic, making latent revert conditions plausible. The bootloader's own comment acknowledges the contract "is available after the upgrade," implying a window before the upgrade where the address is empty (safe) but also implying the contract's correctness is assumed rather than enforced.

---

### Recommendation

Replace the unconditional fatal-error path with a recoverable design:

1. **Degrade gracefully**: If `L2AssetTracker` reverts, log the failure and continue processing the L1→L2 transaction without the asset-tracker notification. Token accounting inconsistency is a lesser harm than a permanent chain halt.

2. **Or introduce a skip mechanism**: Allow the sequencer/operator to mark a specific priority-queue entry as "skip asset-tracker notification" so the chain can advance past a stuck entry, analogous to the `BridgedGovernor` fix of using unordered nonces.

3. **Defensive call**: Wrap the `run_single_interaction` call in a try-catch that converts a revert into a logged warning rather than a fatal error, preserving the chain's liveness.

---

### Proof of Concept

```
1. Identify any revert condition in L2AssetTracker.handleFinalizeBaseTokenBridgingOnL2
   reachable via a specific (fromChainId, amount) pair — e.g., an overflow in
   interopInfo[assetId].totalSuccessfulDepositsFromL1 += amount when the counter
   is near U256::MAX, or a paused/uninitialized state.

2. From L1, submit a priority (L1→L2) transaction with:
   - total_deposited > 0  (so notify_l2_asset_tracker is called)
   - amount crafted to trigger the revert condition

3. The ZKsync OS bootloader processes the priority queue entry:
   a. execute_l1_transaction_and_notify_result runs the main tx body
   b. notify_l2_asset_tracker is called with the crafted amount
   c. L2AssetTracker.handleFinalizeBaseTokenBridgingOnL2 reverts
   d. notify_l2_asset_tracker returns Err(internal_error!(...))
   e. process_l1_transaction propagates the error
   f. tx_loop matches Err(TxError::Internal(err)) => return Err(err)

4. Block processing halts. The priority queue entry remains unprocessed.
   No further blocks can be sealed. The chain is permanently stuck.
   No upgrade can be applied because upgrades are also L1→L2 transactions
   that must pass through the same stuck priority queue.
```

### Citations

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L290-309)
```rust
    mint_base_token::<S, Config>(
        system,
        system_functions,
        memories.reborrow(),
        &pay_to_operator,
        &coinbase,
        l1_chain_id,
        &mut inf_resources,
        tracer,
        validator,
    )
    .map_err(|e| match e.root_cause() {
        RootCause::Runtime(RuntimeError::OutOfErgs(_)) => {
            internal_error!("Out of ergs on infinite ergs").into()
        }
        RootCause::Runtime(RuntimeError::FatalRuntimeError(_)) => {
            internal_error!("Out of native on infinite").into()
        }
        _ => e,
    })?;
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L336-360)
```rust
    if to_refund_recipient > U256::ZERO {
        let refund_recipient = u256_to_b160_checked(transaction.reserved[1].read());
        mint_base_token::<S, Config>(
            system,
            system_functions,
            memories.reborrow(),
            &to_refund_recipient,
            &refund_recipient,
            l1_chain_id,
            &mut inf_resources,
            tracer,
            validator,
        )
        .map_err(|e| -> BootloaderSubsystemError {
            match e.root_cause() {
                RootCause::Runtime(RuntimeError::OutOfErgs(_)) => {
                    internal_error!("Out of ergs on infinite ergs").into()
                }
                RootCause::Runtime(RuntimeError::FatalRuntimeError(_)) => {
                    internal_error!("Out of native on infinite").into()
                }
                _ => e,
            }
        })?;
    }
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L422-432)
```rust
///
/// Compute and perform some checks on fee/resource parameters.
/// This function handles cases that for L2 transactions would be
/// validation errors, as "invalidating" an L1 transaction can halt
/// the chain (due to the priority queue).
/// Note that the "validation errors" are practically unreachable, as
/// gas_limit, gas_price and gas_per_pubdata are either checked or set
/// by the L1 contracts. We decide to handle these cases as a fallback in
/// case the L1 contracts aren't properly updated to reflect a change in
/// ZKsync OS.
/// The approach is to use saturating arithmetic and emit a system
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L757-768)
```rust
    notify_l2_asset_tracker::<S, Config>(
        system,
        system_functions,
        memories,
        *amount,
        l1_chain_id,
        resources,
        tracer,
        validator,
    )?;

    transfer_from_treasury::<S>(system, amount, to, resources, Config::SIMULATION)
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L848-854)
```rust
/// Failure halts block processing — if the asset tracker reverts, the
/// chain's token accounting would be inconsistent, so we treat it as
/// fatal rather than silently continuing with incorrect bookkeeping.
///
/// If no contract is deployed at L2AssetTracker, the call succeeds silently
/// (a call to an empty address returns success with no returndata in EVM).
/// However, we are certain that L2AssetTracker is available after the upgrade.
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L870-912)
```rust
    if amount > U256::ZERO || Config::SIMULATION {
        // Encode calldata for handleFinalizeBaseTokenBridgingOnL2(uint256,uint256):
        // selector 0x03117c8c + abi-encoded (fromChainId, amount)
        let mut calldata = [0u8; 68];
        calldata[0..4].copy_from_slice(&[0x03, 0x11, 0x7c, 0x8c]);
        calldata[4..36].copy_from_slice(&l1_chain_id.to_be_bytes::<32>());
        calldata[36..68].copy_from_slice(&amount.to_be_bytes::<32>());

        let failed = resources.with_infinite_ergs(|inf_ergs| {
            let CompletedExecution {
                resources_returned,
                result: asset_tracker_result,
            } = BasicBootloader::<S, ZkTransactionFlowOnlyEOA<S>>::run_single_interaction(
                system,
                system_functions,
                memories,
                &calldata,
                &L2_BASE_TOKEN_ADDRESS,
                &L2_ASSET_TRACKER_ADDRESS,
                inf_ergs.clone(),
                &U256::ZERO,
                true, // should_make_frame - isolate state changes
                tracer,
                validator,
            )?;
            // Overwrite resources inside the closure so that
            // with_infinite_ergs correctly restores ergs afterwards.
            *inf_ergs = resources_returned;
            Ok::<bool, BootloaderSubsystemError>(asset_tracker_result.failed())
        })?;

        if failed {
            system_log!(
                system,
                "L2AssetTracker.handleFinalizeBaseTokenBridgingOnL2 failed for amount {amount:?}\n"
            );
            // A revert here means the chain's token accounting would be inconsistent.
            // Treated as a fatal system error — block processing cannot continue.
            return Err(internal_error!(
                "L2AssetTracker.handleFinalizeBaseTokenBridgingOnL2 reverted"
            )
            .into());
        }
```

**File:** basic_bootloader/src/bootloader/block_flow/zk/tx_loop.rs (L108-113)
```rust
                        Err(TxError::Internal(err)) => {
                            system_log!(system, "Tx execution result: Internal error = {err:?}\n",);
                            // Finish the frame opened before processing the tx
                            system.finish_global_frame(None)?; // TODO should we use pre_tx_rollback_handle here?
                            return Err(err);
                        }
```
