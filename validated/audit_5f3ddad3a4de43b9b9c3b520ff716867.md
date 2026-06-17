### Title
L2AssetTracker Revert During L1→L2 Deposit Processing Halts Entire Block — (File: `basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs`)

---

### Summary

Every L1→L2 transaction with a non-zero deposit calls `notify_l2_asset_tracker`, which executes `L2AssetTracker.handleFinalizeBaseTokenBridgingOnL2` up to three times. If that call reverts for any reason, the bootloader explicitly treats it as a **fatal system error** and halts block processing entirely. This is the direct analog of the RED token pause blocking `harvestVault()`: an external contract call that can fail under reachable conditions, blocking a critical protocol flow — except here the impact is chain-wide block halting rather than per-user reward blocking.

---

### Finding Description

`notify_l2_asset_tracker` is called from `mint_base_token` for each of the three token movements in an L1→L2 deposit transaction: the value mint to the sender, the operator fee payment, and the refund to the refund recipient. [1](#0-0) 

Inside `notify_l2_asset_tracker`, the bootloader calls `handleFinalizeBaseTokenBridgingOnL2(uint256,uint256)` on the `L2AssetTracker` contract at its fixed address. If the EVM call returns a failure result, the function does not degrade gracefully — it returns a fatal `internal_error!`: [2](#0-1) 

The code comment explicitly documents this design choice:

> *"Failure halts block processing — if the asset tracker reverts, the chain's token accounting would be inconsistent, so we treat it as fatal rather than silently continuing with incorrect bookkeeping."* [3](#0-2) 

This fatal error propagates as `TxError::Internal` up through `process_l1_transaction` → `process_transaction` → `tx_loop`, where it causes the entire block execution to abort: [4](#0-3) 

The `L2AssetTracker` is an upgradeable contract (`OwnableUpgradeable`, `Ownable2StepUpgradeable`) deployed at a fixed address. Its `handleFinalizeBaseTokenBridgingOnL2` function checks several internal state conditions — asset registration, migration number, settlement layer chain ID — any of which can cause a revert. Conditions that can produce a revert include:

- The base token asset not being registered (`isAssetRegistered[assetId] == false`)
- A migration number mismatch (`assetMigrationNumber[chainId][assetId]`)
- A governance upgrade that introduces a bug or adds a pause mechanism
- The `SystemContext.currentSettlementLayerChainId()` returning an unexpected value

The storage layout of `L2AssetTracker` is read directly by the bootloader (slot 154 for `L1_CHAIN_ID`), confirming the contract is a live, stateful dependency: [5](#0-4) 

The three call sites that can each independently trigger the fatal path are:

1. Value mint to sender (inside the execution frame): [6](#0-5) 

2. Operator fee payment (post-execution, `FORMAL_INFINITE` resources): [7](#0-6) 

3. Refund to refund recipient: [8](#0-7) 

---

### Impact Explanation

**High.** Any L1→L2 transaction with `total_deposited > 0` that arrives when `L2AssetTracker.handleFinalizeBaseTokenBridgingOnL2` reverts will cause the bootloader to abort block processing entirely. This halts the chain's state transition function — no further transactions in the block can be processed. Because L1→L2 transactions are priority queue entries that cannot be invalidated (the code explicitly notes *"invalidating an L1 transaction can halt the chain"*), the block cannot simply skip the offending transaction. The entire block fails. [9](#0-8) 

---

### Likelihood Explanation

**Low.** The `L2AssetTracker` must be in a state where `handleFinalizeBaseTokenBridgingOnL2` reverts. This requires either a governance upgrade that introduces a revert path, a pause mechanism being activated, or the contract's internal state (asset registration, migration number) becoming inconsistent. These are rare but realistic events — governance upgrades happen, and the contract is explicitly upgradeable. Once the condition exists, any unprivileged user submitting an L1→L2 deposit transaction becomes the trigger.

---

### Recommendation

Apply the same fix class recommended in the external report: handle the `L2AssetTracker` revert gracefully rather than treating it as a chain-halting fatal error. Concretely:

1. **Degrade gracefully on revert**: If `handleFinalizeBaseTokenBridgingOnL2` reverts, emit a system log and continue processing (accepting that the accounting entry is missed) rather than aborting the block. The accounting inconsistency concern is real but less severe than a halted chain.
2. **Or, skip the notification on revert**: Treat a revert from `L2AssetTracker` the same way an empty-address call is treated (silent success), and rely on off-chain reconciliation or a separate recovery mechanism.
3. **Alternatively, add a circuit-breaker**: Allow the operator to mark the `L2AssetTracker` notification as optional via a system flag, so that if the contract is in a bad state, L1→L2 transactions can still be processed.

---

### Proof of Concept

1. The `L2AssetTracker` is upgraded (governance action) to a version that reverts on `handleFinalizeBaseTokenBridgingOnL2` — e.g., a pause is activated, or the asset registration is cleared.
2. An unprivileged user submits an L1→L2 transaction with `total_deposited > 0` (any standard deposit from L1).
3. The bootloader calls `mint_base_token` → `notify_l2_asset_tracker` → `run_single_interaction` targeting `L2AssetTracker`.
4. The EVM call returns `failed = true`.
5. `notify_l2_asset_tracker` returns `Err(internal_error!("L2AssetTracker.handleFinalizeBaseTokenBridgingOnL2 reverted"))`.
6. This propagates as `TxError::Internal(err)` to `tx_loop`.
7. `tx_loop` matches `Err(TxError::Internal(err))` and executes `return Err(err)`, aborting the entire block.
8. The chain's state transition function fails; no block can be sealed until the `L2AssetTracker` is fixed via another governance upgrade. [10](#0-9) [11](#0-10)

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

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L336-359)
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
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L422-431)
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
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L631-658)
```rust
    if to_transfer > U256::ZERO || Config::SIMULATION {
        resources
            .with_infinite_ergs(|inf_resources| {
                mint_base_token::<S, Config>(
                    system,
                    system_functions,
                    memories.reborrow(),
                    &to_transfer,
                    &from,
                    l1_chain_id,
                    inf_resources,
                    tracer,
                    validator,
                )
            })
            .map_err(|e| match e.root_cause() {
                RootCause::Runtime(RuntimeError::OutOfErgs(_)) => {
                    system_log!(
                        system,
                        "Out of ergs on infinite ergs: inner error was {e:?}"
                    );
                    BootloaderSubsystemError::LeafDefect(internal_error!(
                        "Out of ergs on infinite ergs"
                    ))
                }
                _ => e,
            })?;
    }
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

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L925-943)
```rust
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
