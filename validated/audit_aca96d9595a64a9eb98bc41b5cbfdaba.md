### Title
L1→L2 Deposit Finalization Halts Entire Block on L2AssetTracker Revert with No Fallback Mechanism - (File: `basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs`)

---

### Summary

The `notify_l2_asset_tracker` function in `process_l1_transaction.rs` unconditionally treats any revert from `L2AssetTracker.handleFinalizeBaseTokenBridgingOnL2` as a fatal system error that permanently halts all block processing. Any L1→L2 priority transaction with a non-zero deposit triggers this call. If the L2AssetTracker contract is in a state where it reverts (e.g., asset not registered, migration number mismatch, or settlement-layer chain ID mismatch), the entire block processing pipeline halts with no fallback or recovery path available to users or the sequencer.

---

### Finding Description

During L1→L2 deposit processing, `process_l1_transaction` calls `mint_base_token`, which in turn calls `notify_l2_asset_tracker`. This function dispatches `handleFinalizeBaseTokenBridgingOnL2(uint256 _fromChainId, uint256 _amount)` to the `L2AssetTracker` contract at address `0x800b`.

The critical code path is:

```
process_l1_transaction
  └─ execute_l1_transaction_and_notify_result  (value-mint call)
       └─ mint_base_token
            └─ notify_l2_asset_tracker   ← fatal if reverts
  └─ mint_base_token (operator fee)      ← fatal if reverts
  └─ mint_base_token (refund)            ← fatal if reverts
```

The `notify_l2_asset_tracker` function explicitly documents and implements this fatal behavior:

```rust
if failed {
    // A revert here means the chain's token accounting would be inconsistent.
    // Treated as a fatal system error — block processing cannot continue.
    return Err(internal_error!(
        "L2AssetTracker.handleFinalizeBaseTokenBridgingOnL2 reverted"
    ).into());
}
```

The `L2AssetTracker` contract enforces several state-dependent checks before recording a deposit:
- `isAssetRegistered[assetId]` must be `true`
- `assetMigrationNumber[chainId][assetId]` must match the expected value
- The settlement-layer chain ID (read from `SystemContext` slot 0) must match `L1_CHAIN_ID` (read from L2AssetTracker slot 154)

These values are read directly from storage by the bootloader at runtime:

```rust
let l1_chain_id_slot = Bytes32::from_u256_be(&U256::from(154));
let chain_id = system.io.storage_read::<false>(
    ExecutionEnvironmentType::NoEE,
    &mut inf_resources,
    &L2_ASSET_TRACKER_ADDRESS,
    &l1_chain_id_slot,
).expect("must read L2AssetTracker L1_CHAIN_ID");
```

If any of these storage values are inconsistent (e.g., during a protocol upgrade, migration, or misconfiguration), the L2AssetTracker reverts, and the bootloader immediately escalates to a fatal block-level error. There is no graceful degradation, no per-transaction isolation, and no recovery path.

This is called up to **three times per L1→L2 deposit** (value mint, operator fee, refund), each with the same fatal-on-revert behavior.

---

### Impact Explanation

**Chain halt**: When `notify_l2_asset_tracker` returns a fatal error, `process_l1_transaction` propagates it as a `BootloaderSubsystemError` (not a `TxError::Validation`). Since L1 priority transactions cannot be invalidated or skipped, the block processing loop in `tx_loop.rs` cannot continue. The entire block fails to finalize.

This is more severe than the sUSX analog: instead of locking one user's funds, it halts the entire ZKsync OS chain. All subsequent L1→L2 deposits in the priority queue are also blocked. Users who have already locked tokens on L1 cannot receive them on L2, and the chain cannot produce new blocks until an admin corrects the L2AssetTracker state.

The `test_treasury_insufficient_balance_failure` test confirms the block-level failure pattern: `execute_block_no_panic` returns `Err(...)` (not a per-tx error) when a mandatory post-execution operation fails.

---

### Likelihood Explanation

**Medium**. The L2AssetTracker state is set by governance and is expected to be correct in steady-state operation. However, the following realistic scenarios can trigger the revert:

1. **Protocol upgrade window**: During a protocol upgrade that changes the `assetMigrationNumber`, there is a window where the L2AssetTracker's stored migration number does not match what the chain expects. Any L1→L2 deposit submitted during this window triggers the fatal halt.

2. **Settlement-layer chain ID mismatch**: The `SystemContext` settlement-layer chain ID (slot 0) is compared against the L2AssetTracker's `L1_CHAIN_ID` (slot 154). If these are updated non-atomically during a migration, a mismatch causes a revert.

3. **Unregistered asset**: If `isAssetRegistered[BASE_TOKEN_ASSET_ID]` is `false` (e.g., after a fresh deployment before initialization), every L1→L2 deposit halts the chain.

The trigger (submitting an L1→L2 deposit) requires no privileged access — any user who has locked tokens on L1 can submit a priority transaction. The precondition (L2AssetTracker in a bad state) can arise from misconfiguration or upgrade sequencing errors.

---

### Recommendation

1. **Decouple the fatal error from per-transaction processing**: Instead of halting block processing when `notify_l2_asset_tracker` reverts, treat the failure as a per-transaction error. For priority (L1→L2) transactions, this means recording the failure in the L2→L1 log so the L1 bridge can initiate a refund, analogous to how failed L1→L2 transactions are handled in the existing refund path.

2. **Add a fallback path**: If `handleFinalizeBaseTokenBridgingOnL2` reverts, the bootloader should still complete the treasury transfer and emit the L1→L2 log with a failure status, rather than aborting block processing entirely.

3. **Validate L2AssetTracker state before processing deposits**: Add a pre-check that verifies the L2AssetTracker is in a valid state before attempting the call, and skip the notification (with a logged warning) if the contract is not ready.

---

### Proof of Concept

The following scenario demonstrates the chain halt:

**Setup**: Deploy a chain where `L2AssetTracker.isAssetRegistered[BASE_TOKEN_ASSET_ID]` is `false` (e.g., after a fresh deployment or during a migration where the asset registration was not yet completed).

**Trigger**: Submit any L1→L2 priority transaction with `total_deposited > 0`:

```rust
let l1_tx = L1TxBuilder::new()
    .from(attacker)
    .to(any_address)
    .gas_price(1000)
    .gas_limit(100_000)
    .value(U256::from(1u64))
    .to_mint(U256::from(1_000_000u64))  // non-zero deposit triggers notify_l2_asset_tracker
    .build();

let result = tester
    .without_asset_registration()  // L2AssetTracker.isAssetRegistered = false
    .execute_block_no_panic(vec![l1_tx]);

// result is Err(...) containing "L2AssetTracker.handleFinalizeBaseTokenBridgingOnL2 reverted"
// Block processing halted — no further transactions can be processed
```

**Root cause trace**:
1. `process_l1_transaction` calls `execute_l1_transaction_and_notify_result` [1](#0-0) 
2. Inside, `mint_base_token` calls `notify_l2_asset_tracker` [2](#0-1) 
3. `notify_l2_asset_tracker` dispatches `handleFinalizeBaseTokenBridgingOnL2` and checks the result [3](#0-2) 
4. On revert, it returns a fatal `internal_error!` that propagates as `BootloaderSubsystemError` [4](#0-3) 
5. The same fatal path applies to the post-execution operator-fee and refund `mint_base_token` calls [5](#0-4) 
6. The L2AssetTracker state prerequisites (isAssetRegistered, assetMigrationNumber, chain ID) are set by governance and read at runtime from storage [6](#0-5)

### Citations

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L186-200)
```rust
            match execute_l1_transaction_and_notify_result::<S, Config>(
                system,
                system_functions,
                &mut memories,
                &transaction,
                from,
                to,
                value,
                l1_chain_id,
                native_per_pubdata,
                &mut resources,
                withheld_resources,
                tracer,
                validator,
            ) {
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

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L878-912)
```rust
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
