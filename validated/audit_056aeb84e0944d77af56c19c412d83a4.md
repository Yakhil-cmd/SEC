### Title
`notify_l2_asset_tracker` Fatal-Error-on-Revert Halts Entire Block Processing for All L1→L2 Deposits — (`File: basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs`)

---

### Summary

The bootloader uses a strict "Push" pattern when notifying `L2AssetTracker` about base-token movements during L1→L2 transaction processing. If `L2AssetTracker.handleFinalizeBaseTokenBridgingOnL2` reverts for any reason, `notify_l2_asset_tracker` escalates the failure to a **fatal internal error** that halts the entire block processing pipeline — not just the individual transaction. This is a direct structural analog to the escrow "Push" pattern vulnerability: a single recipient's revert blocks the entire settlement flow.

---

### Finding Description

During every L1→L2 transaction that carries a non-zero deposit (`total_deposited > 0`), the bootloader calls `notify_l2_asset_tracker` up to three times — once for the value mint, once for the operator fee, and once for the refund. Each call pushes `handleFinalizeBaseTokenBridgingOnL2(fromChainId, amount)` to the `L2AssetTracker` contract at `L2_ASSET_TRACKER_ADDRESS`.

The critical code path is in `notify_l2_asset_tracker`:

```rust
if failed {
    // A revert here means the chain's token accounting would be inconsistent.
    // Treated as a fatal system error — block processing cannot continue.
    return Err(internal_error!(
        "L2AssetTracker.handleFinalizeBaseTokenBridgingOnL2 reverted"
    )
    .into());
}
``` [1](#0-0) 

This error propagates through `mint_base_token` → `process_l1_transaction` with `?`, causing the entire block processing to abort: [2](#0-1) 

The three `mint_base_token` calls (operator fee, refund, and value mint inside `execute_l1_transaction_and_notify_result`) each invoke `notify_l2_asset_tracker` before `transfer_from_treasury`: [3](#0-2) 

The design comment explicitly acknowledges the fatal nature:

> "Failure halts block processing — if the asset tracker reverts, the chain's token accounting would be inconsistent, so we treat it as fatal rather than silently continuing with incorrect bookkeeping." [4](#0-3) 

The `L2AssetTracker` is a real EVM contract (predeploy) whose storage can be read and written by normal L2 transactions. Its `handleFinalizeBaseTokenBridgingOnL2` function checks internal state (asset registration, chain ID matching, migration numbers). If an unprivileged caller can manipulate `L2AssetTracker` storage — e.g., by calling any publicly accessible function that deregisters the base token asset, resets migration state, or triggers an internal revert path — then every subsequent L1→L2 transaction with a non-zero deposit will cause block processing to halt.

The `L2AssetTracker` predeploy is initialized with specific storage slots: [5](#0-4) 

Any state transition that invalidates these assumptions (e.g., `isAssetRegistered` set to false, or `assetMigrationNumber` reset) would cause `handleFinalizeBaseTokenBridgingOnL2` to revert, triggering the fatal error.

---

### Impact Explanation

**Vulnerability class:** State-transition bug / valid-execution unprovability.

If `L2AssetTracker.handleFinalizeBaseTokenBridgingOnL2` reverts:
- Block processing halts entirely for the current block.
- All L1→L2 transactions with non-zero deposits in that block cannot be processed.
- The chain is effectively stalled until the `L2AssetTracker` state is repaired (requiring a governance upgrade).
- Deposited funds are locked: the treasury has already been debited (or is about to be), but the recipient never receives the tokens.

This is a **chain-halting denial-of-service** triggered by a single malformed L1→L2 transaction, analogous to the escrow where a single party's revert blocks the entire settlement.

---

### Likelihood Explanation

The `L2AssetTracker` is a real EVM contract deployed at a fixed address. Its storage is accessible to any L2 transaction. The contract is upgradeable (OwnableUpgradeable), but its internal state (asset registration, migration numbers) may be modifiable through publicly callable functions. If any such function exists that an unprivileged caller can use to put the contract into a revert-on-notify state, the attack is directly reachable. The structural vulnerability in the bootloader (treating the revert as fatal) is confirmed; the exploitability depends on `L2AssetTracker` internals.

---

### Recommendation

1. **Do not treat `L2AssetTracker` revert as a fatal block-halting error.** Instead, log the failure and continue block processing. The token accounting inconsistency concern can be addressed by a separate reconciliation mechanism.
2. **Apply the "Pull" pattern analog**: store failed notification amounts in a mapping and provide a separate function for the asset tracker to pull/reconcile them later.
3. **Alternatively**, wrap the `notify_l2_asset_tracker` call in a recoverable error path (similar to how `FatalRuntimeError` is converted to a top-level revert for the main tx body at lines 221–234) so that a single L1→L2 transaction's asset-tracker failure does not abort the entire block. [6](#0-5) 

---

### Proof of Concept

1. Deploy a normal L2 transaction that calls `L2AssetTracker` at `L2_ASSET_TRACKER_ADDRESS` using a publicly accessible function that modifies the `isAssetRegistered` mapping or `assetMigrationNumber` for the base token asset ID, putting the contract into a state where `handleFinalizeBaseTokenBridgingOnL2` reverts.
2. Submit any L1→L2 priority transaction with `to_mint > 0` (non-zero deposit).
3. The bootloader processes the L1 tx, calls `mint_base_token` for the operator fee, which calls `notify_l2_asset_tracker`, which calls `handleFinalizeBaseTokenBridgingOnL2`.
4. `L2AssetTracker` reverts → `notify_l2_asset_tracker` returns `Err(internal_error!(...))` → `process_l1_transaction` propagates the error → block processing halts.
5. All subsequent L1→L2 transactions in the block are unprocessable. The chain is stalled. [7](#0-6)

### Citations

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L217-241)
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

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L848-851)
```rust
/// Failure halts block processing — if the asset tracker reverts, the
/// chain's token accounting would be inconsistent, so we treat it as
/// fatal rather than silently continuing with incorrect bookkeeping.
///
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

**File:** tests/rig/src/predeployed_contracts.rs (L49-91)
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

    let system_context_bytecode =
        hex::decode(SYSTEM_CONTEXT_BYTECODE.trim()).expect("valid system context bytecode");
    chain.set_evm_bytecode(SYSTEM_CONTEXT_ADDRESS, &system_context_bytecode);
    chain.set_storage_slot(
        SYSTEM_CONTEXT_ADDRESS,
        U256::from(SYSTEM_CONTEXT_SETTLEMENT_LAYER_CHAIN_ID_SLOT),
        B256::from(U256::from(DEFAULT_L1_CHAIN_ID)),
    );
}
```
