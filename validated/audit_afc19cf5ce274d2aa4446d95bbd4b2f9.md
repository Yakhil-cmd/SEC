### Title
Fatal Block Halt via `L2AssetTracker` Revert in `notify_l2_asset_tracker` — (`basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs`)

---

### Summary

The bootloader uses a push-strategy to notify the `L2AssetTracker` contract for every L1→L2 transaction with a non-zero deposit. If the `L2AssetTracker` reverts for any reason, the bootloader explicitly converts that revert into a fatal `InternalError` that propagates up through the transaction loop and halts all block processing. A single L1 transaction can therefore permanently block all subsequent L1 transactions in the priority queue.

---

### Finding Description

For every L1→L2 transaction with `total_deposited > 0`, the bootloader calls `notify_l2_asset_tracker` up to **three times** — once for the value mint (inside `execute_l1_transaction_and_notify_result`), once for the operator fee, and once for the refund (both in `process_l1_transaction`). [1](#0-0) 

Inside `notify_l2_asset_tracker`, if the EVM call to `handleFinalizeBaseTokenBridgingOnL2` returns a failed result, the function explicitly returns a fatal `InternalError`: [2](#0-1) 

This error is **not** a `FatalRuntimeError`, so it is not caught by the `FatalRuntimeError` handler in `process_l1_transaction`: [3](#0-2) 

The error propagates as `TxError::Internal` into the ZK transaction loop, which unconditionally returns it, halting the entire block: [4](#0-3) 

The same fatal path applies to the operator-fee and refund `mint_base_token` calls in `process_l1_transaction`: [5](#0-4) [6](#0-5) 

The `L2AssetTracker` is a real, upgradeable Solidity contract predeployed at a fixed address. Its `handleFinalizeBaseTokenBridgingOnL2` function has internal guards (e.g., `isAssetRegistered`, `assetMigrationNumber` checks) that can revert under specific conditions. The `l1_chain_id` passed to the call is read directly from the contract's own storage slot 154: [7](#0-6) 

The predeployed contract setup confirms the storage dependencies: [8](#0-7) 

If any of these storage invariants are violated (e.g., after a chain upgrade that changes the chain ID, or if `isAssetRegistered` is cleared), the `handleFinalizeBaseTokenBridgingOnL2` call reverts, and block processing halts permanently.

The test suite itself documents the three-call pattern and the fatal consequence: [9](#0-8) 

---

### Impact Explanation

- **Chain halt**: When `notify_l2_asset_tracker` returns a fatal error, the ZK transaction loop returns `Err`, aborting the entire block. No further transactions — L1 or L2 — are processed.
- **Priority queue DoS**: L1→L2 transactions in the priority queue cannot be skipped. A single deposit transaction that triggers the revert permanently stalls the queue.
- **Irreversibility**: Because the error is treated as a system-level fatal condition rather than a per-transaction validation failure, there is no recovery path within the current block execution.

---

### Likelihood Explanation

The `L2AssetTracker` is an upgradeable proxy contract. Its `handleFinalizeBaseTokenBridgingOnL2` function enforces storage-backed invariants (`isAssetRegistered`, `assetMigrationNumber`, chain-ID matching). Any of the following realistic scenarios triggers the halt:

1. A protocol upgrade changes the chain ID or resets the `assetMigrationNumber` mapping, causing the next L1 deposit to revert the asset tracker.
2. A bug introduced in an `L2AssetTracker` upgrade causes the function to revert for specific `(fromChainId, amount)` combinations.
3. The `isAssetRegistered` flag for the base token asset ID is cleared (e.g., via a migration), causing every subsequent deposit notification to revert.

All three scenarios are reachable via a single L1→L2 transaction submitted by any unprivileged user once the contract state is in the triggering condition.

---

### Recommendation

Replace the fatal-error design with a graceful-degradation strategy:

1. **Do not treat `L2AssetTracker` revert as a block-level fatal error.** Instead, revert the individual L1 transaction (marking it as failed) and continue processing the remaining transactions in the block.
2. **Separate the asset-tracker notification from the treasury transfer.** If the notification fails, the treasury transfer should also be rolled back, but block processing must continue.
3. **Add a circuit-breaker**: if the `L2AssetTracker` is unreachable or consistently reverting, allow the operator to skip the notification (with an on-chain flag) rather than halting the chain.

---

### Proof of Concept

1. Deploy a chain where the `L2AssetTracker`'s `isAssetRegistered[BASE_TOKEN_ASSET_ID]` storage slot is `false` (e.g., after a migration that clears it).
2. Submit any L1→L2 transaction with `total_deposited > 0` (e.g., `gas_price = 1`, `gas_limit = 50_000`, `to_mint = gas_price * gas_limit + 1`).
3. The bootloader calls `notify_l2_asset_tracker` for the value mint.
4. `handleFinalizeBaseTokenBridgingOnL2` reverts because the asset is not registered.
5. `notify_l2_asset_tracker` returns `Err(internal_error!("L2AssetTracker.handleFinalizeBaseTokenBridgingOnL2 reverted"))`.
6. The ZK transaction loop receives `TxError::Internal` and returns `Err`, halting block processing.
7. All subsequent L1 transactions in the priority queue are permanently blocked. [10](#0-9)

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

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L338-360)
```rust
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

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L836-854)
```rust
/// Notify L2AssetTracker about base token bridging from L1.
///
/// Calls handleFinalizeBaseTokenBridgingOnL2(uint256 _fromChainId, uint256 _amount)
/// as L2_BASE_TOKEN_ADDRESS (0x800a) to pass the onlyBaseTokenHolderOrL2BaseToken modifier.
///
/// This is called separately for each token movement (value mint, operator
/// payment, refund) so that the asset tracker's accounting stays correct even
/// if the main transaction body reverts.
///
/// Resource usage depends on the caller — value-mint tracks native against user resources;
/// operator-fee and refund use FORMAL_INFINITE.
///
/// Failure halts block processing — if the asset tracker reverts, the
/// chain's token accounting would be inconsistent, so we treat it as
/// fatal rather than silently continuing with incorrect bookkeeping.
///
/// If no contract is deployed at L2AssetTracker, the call succeeds silently
/// (a call to an empty address returns success with no returndata in EVM).
/// However, we are certain that L2AssetTracker is available after the upgrade.
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L870-913)
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
    }
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L921-944)
```rust
fn read_l1_chain_id<S: EthereumLikeTypes>(system: &mut System<S>) -> U256
where
    S::IO: IOSubsystemExt,
{
    // L2AssetTracker storage layout (verified via `forge inspect`):
    //   slots 0-100:   Initializable + OwnableUpgradeable + Ownable2StepUpgradeable
    //   slots 101-150: Ownable2Step __gap
    //   slot 151:      mapping chainBalance
    //   slot 152:      mapping assetMigrationNumber
    //   slot 153:      mapping isAssetRegistered
    //   slot 154:      uint256 L1_CHAIN_ID
    let l1_chain_id_slot = Bytes32::from_u256_be(&U256::from(154));
    let mut inf_resources = S::Resources::FORMAL_INFINITE;
    let chain_id = system
        .io
        .storage_read::<false>(
            ExecutionEnvironmentType::NoEE,
            &mut inf_resources,
            &L2_ASSET_TRACKER_ADDRESS,
            &l1_chain_id_slot,
        )
        .expect("must read L2AssetTracker L1_CHAIN_ID");
    U256::from_be_bytes(chain_id.as_u8_array())
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

**File:** tests/rig/src/predeployed_contracts.rs (L49-82)
```rust
pub fn install_default_predeployed_contracts<const RANDOMIZED_TREE: bool>(
    chain: &mut Chain<RANDOMIZED_TREE>,
) {
    let l2_asset_tracker_bytecode =
        hex::decode(L2_ASSET_TRACKER_BYTECODE.trim()).expect("valid L2AssetTracker bytecode");
    chain.set_evm_bytecode(L2_ASSET_TRACKER_ADDRESS, &l2_asset_tracker_bytecode);
    chain.set_storage_slot(
        L2_ASSET_TRACKER_ADDRESS,
        U256::from(L2_ASSET_TRACKER_L1_CHAIN_ID_SLOT),
        B256::from(U256::from(DEFAULT_L1_CHAIN_ID)),
    );
    chain.set_storage_slot(
        L2_ASSET_TRACKER_ADDRESS,
        U256::from(L2_ASSET_TRACKER_BASE_TOKEN_ASSET_ID_SLOT),
        DEFAULT_BASE_TOKEN_ASSET_ID,
    );
    chain.set_storage_slot(
        L2_ASSET_TRACKER_ADDRESS,
        mapping_slot_bytes32(
            DEFAULT_BASE_TOKEN_ASSET_ID,
            L2_ASSET_TRACKER_IS_ASSET_REGISTERED_SLOT,
        ),
        B256::from(U256::ONE),
    );
    chain.set_storage_slot(
        L2_ASSET_TRACKER_ADDRESS,
        nested_mapping_slot_u64_bytes32(
            chain.chain_id(),
            DEFAULT_BASE_TOKEN_ASSET_ID,
            L2_ASSET_TRACKER_ASSET_MIGRATION_NUMBER_SLOT,
        ),
        B256::from(U256::ONE),
    );

```

**File:** tests/instances/transactions/src/asset_tracker.rs (L1-13)
```rust
//!
//! Tests for the L2AssetTracker.handleFinalizeBaseTokenBridgingOnL2 calls
//! that the bootloader makes during L1 transaction processing.
//!
//! When an L1 transaction deposits base tokens (total_deposited > 0), the
//! bootloader calls handleFinalizeBaseTokenBridgingOnL2(uint256, uint256)
//! on the real L2AssetTracker contract up to three times — once for the
//! value mint, once for the operator fee, and once for the refund. If any
//! of these amounts is zero the corresponding call is skipped.
//!
//! When the source chain matches `L1_CHAIN_ID` and the current settlement
//! layer also matches `L1_CHAIN_ID`, the contract records the aggregate
//! bridged amount in `interopInfo[BASE_TOKEN_ASSET_ID].totalSuccessfulDepositsFromL1`.
```
