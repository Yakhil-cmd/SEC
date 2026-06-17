### Title
L1→L2 Deposit Block Processing Permanently Halted by `L2AssetTracker.handleFinalizeBaseTokenBridgingOnL2` Revert — (`basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs`)

---

### Summary

The bootloader's `notify_l2_asset_tracker` function calls the external `L2AssetTracker` contract during every L1→L2 deposit transaction. Any revert from that contract is treated as a fatal internal error that halts block processing entirely. Because L1 transactions cannot be invalidated (they come from the priority queue), a single deposit transaction that causes `L2AssetTracker` to revert permanently stalls the chain.

---

### Finding Description

During L1→L2 deposit processing, `process_l1_transaction` calls `mint_base_token`, which in turn calls `notify_l2_asset_tracker`. This function executes `L2AssetTracker.handleFinalizeBaseTokenBridgingOnL2(uint256 _fromChainId, uint256 _amount)` as a live EVM call to the contract deployed at `0x1000f`. [1](#0-0) 

If the call returns a failed result, the function explicitly converts it to a fatal `BootloaderSubsystemError`:

```rust
if failed {
    // Treated as a fatal system error — block processing cannot continue.
    return Err(internal_error!(
        "L2AssetTracker.handleFinalizeBaseTokenBridgingOnL2 reverted"
    ).into());
}
``` [2](#0-1) 

`internal_error!` produces an `InternalError` whose `root_cause()` is `RootCause::Internal`. In the caller `process_l1_transaction`, the error-handling match arm only recovers gracefully from `RootCause::Runtime(FatalRuntimeError(_))`; all other root causes fall through to `_ => { return Err(e); }`, propagating the error upward: [3](#0-2) 

The block-level transaction loop in `tx_loop.rs` then receives a `TxError::Internal` and immediately returns the error, halting the entire block: [4](#0-3) 

`notify_l2_asset_tracker` is called up to **three times** per deposit transaction — once for the value mint (inside the execution frame), once for the operator fee, and once for the refund: [5](#0-4) [6](#0-5) [7](#0-6) 

The `_amount` argument passed to `handleFinalizeBaseTokenBridgingOnL2` is derived directly from attacker-controlled L1 transaction fields: `total_deposited`, `gas_price`, and `gas_limit`. The `_fromChainId` is read from `L2AssetTracker` storage slot 154. Any revert condition inside the `L2AssetTracker` contract — whether triggered by specific input values, a storage state mismatch (e.g., `isAssetRegistered`, `assetMigrationNumber`), or a contract bug — will cause the fatal halt.

The code comment itself acknowledges the design choice and its consequence:

> *"Failure halts block processing — if the asset tracker reverts, the chain's token accounting would be inconsistent, so we treat it as fatal rather than silently continuing with incorrect bookkeeping."* [8](#0-7) 

---

### Impact Explanation

- Any L1→L2 deposit transaction (`total_deposited > 0`) that causes `L2AssetTracker.handleFinalizeBaseTokenBridgingOnL2` to revert will halt block processing with a fatal internal error.
- L1 transactions cannot be invalidated or skipped (doing so would halt the priority queue). There is no recovery path.
- Deposited funds are locked on L1 with no corresponding credit on L2 and no refund mechanism.
- The chain is permanently stalled at the block containing the offending transaction.

---

### Likelihood Explanation

- `L2AssetTracker` is a regular EVM contract with multiple internal revert conditions (`isAssetRegistered`, `assetMigrationNumber` checks, etc.).
- The `_amount` and `_fromChainId` arguments are partially attacker-controlled via L1 transaction parameters.
- Any future upgrade to `L2AssetTracker` that introduces a new revert condition, or any edge case in the existing contract triggered by a specific deposit amount, immediately becomes a chain-halting vector.
- The attack requires only submitting a standard L1→L2 deposit transaction — no privileged access is needed.

---

### Recommendation

- **Decouple block liveness from `L2AssetTracker` revert behavior**: treat a revert from `handleFinalizeBaseTokenBridgingOnL2` as a non-fatal accounting discrepancy rather than a chain-halting error. Log the failure and continue block processing, then reconcile accounting out-of-band.
- **Alternatively**: give the bootloader a special bypass role in `L2AssetTracker` so that calls originating from the bootloader context cannot be reverted by internal contract logic.
- **At minimum**: distinguish between "contract not deployed" (currently handled gracefully) and "contract reverted" — the former is already treated as a no-op success; the latter should have a similar fallback rather than a fatal halt.

---

### Proof of Concept

1. Attacker submits an L1→L2 deposit transaction with `total_deposited > 0` and parameters crafted to produce an `_amount` value that triggers a revert condition inside `L2AssetTracker.handleFinalizeBaseTokenBridgingOnL2`.
2. The bootloader calls `notify_l2_asset_tracker` with the derived `amount`.
3. `L2AssetTracker.handleFinalizeBaseTokenBridgingOnL2` reverts (EVM-level revert).
4. `notify_l2_asset_tracker` detects `asset_tracker_result.failed() == true` and returns `Err(internal_error!(...))`.
5. The error propagates through `mint_base_token` → `execute_l1_transaction_and_notify_result` → `process_l1_transaction` (falling through the `_ => { return Err(e); }` arm because `RootCause::Internal` does not match `FatalRuntimeError`).
6. The block-level loop receives `TxError::Internal` and returns the error, halting block processing.
7. The chain cannot produce any further blocks; all subsequent L1 deposit transactions in the priority queue are also stuck.

### Citations

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L217-240)
```rust
                Err(e) => {
                    match e.root_cause() {
                        // Out of native / memory is converted to a top-level
                        // revert so post-execution L1 accounting can still run.
                        RootCause::Runtime(runtime @ RuntimeError::FatalRuntimeError(_)) => {
                            system_log!(
                                system,
                                "L1 transaction ran out of native resources or memory {runtime:?}\n"
                            );
                            resources.exhaust_ergs();
                            system.finish_global_frame(Some(&rollback_handle))?;
                            (
                                false,
                                Vec::new_in(system.get_allocator()),
                                None,
                                S::Resources::empty(),
                                memories,
                            )
                        }
                        _ => {
                            system.finish_global_frame(Some(&rollback_handle))?;
                            return Err(e);
                        }
                    }
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L288-310)
```rust
    let coinbase = system.get_coinbase();
    // Mint operator fee portion of the deposit to coinbase.
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

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L757-769)
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
}
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
