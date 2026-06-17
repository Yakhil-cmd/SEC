### Title
`notify_l2_asset_tracker` Unconditionally Halts Block Processing on `L2AssetTracker` Revert — (`File: basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs`)

---

### Summary

Every L1→L2 deposit transaction triggers a call to `L2AssetTracker.handleFinalizeBaseTokenBridgingOnL2` via `notify_l2_asset_tracker`. If that external contract call reverts for any reason, the bootloader treats it as a fatal internal error and halts block processing entirely. This is a direct analog to the reported pattern: an external contract callback whose revert propagates upward and blocks a critical system flow.

---

### Finding Description

`notify_l2_asset_tracker` is called up to three times per L1→L2 transaction (once for the value mint, once for the operator fee, once for the refund). It executes `handleFinalizeBaseTokenBridgingOnL2(uint256 _fromChainId, uint256 _amount)` on the `L2AssetTracker` contract at `L2_ASSET_TRACKER_ADDRESS`, spoofing the caller as `L2_BASE_TOKEN_ADDRESS` (0x800a) to pass the `onlyBaseTokenHolderOrL2BaseToken` modifier.

The critical path is:

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

There is no try/catch, no graceful degradation, and no skip-on-failure path. Any revert from `L2AssetTracker` — whether from an edge-case in its Solidity logic, an unregistered asset, an uninitialized state, or a future upgrade introducing new revert conditions — unconditionally propagates as a fatal `InternalError` that terminates block processing.

The `l1_chain_id` passed to the call is read directly from `L2AssetTracker` storage slot 154: [2](#0-1) 

The `amount` is derived from the L1 transaction's `to_mint` / deposit fields, which are fully attacker-controlled. The call is made with `should_make_frame = true` (state isolation), but the *result* is not isolated — a revert still propagates fatally. [3](#0-2) 

The function is called from `mint_base_token`, which is called from `process_l1_transaction` for every L1→L2 deposit: [4](#0-3) 

The developer comment explicitly acknowledges the fatal treatment: [5](#0-4) 

---

### Impact Explanation

If `L2AssetTracker.handleFinalizeBaseTokenBridgingOnL2` reverts for any L1→L2 deposit transaction, the entire block cannot be finalized. This is a **chain-halting DoS**: no further transactions in the block can be processed, and the sequencer cannot produce a valid block. The impact is more severe than the external report's analog (which only blocked three specific functions); here the entire block processing pipeline is halted.

---

### Likelihood Explanation

The `L2AssetTracker` is a Solidity contract that can revert under several realistic conditions:

1. **Unregistered asset**: If the base token asset ID is not registered in `L2AssetTracker` at the time of the call, the contract may revert.
2. **Uninitialized state**: If `L1_CHAIN_ID` (slot 154) is zero or unset, the contract's internal logic may revert.
3. **Future upgrade**: `L2AssetTracker` is an upgradeable contract. A future upgrade introducing new access control, state checks, or invariant enforcement could introduce revert conditions for previously valid inputs.
4. **Edge-case amounts**: Specific `amount` values (e.g., `U256::MAX`) could trigger overflow checks inside the Solidity contract.

Any unprivileged user can submit an L1→L2 transaction with `to_mint > 0`, triggering this code path. The attacker does not need any privileged role — only the ability to submit an L1→L2 transaction.

---

### Recommendation

Replace the unconditional fatal error with a graceful handling strategy:

1. **Do not treat `L2AssetTracker` reverts as fatal for block processing.** Instead, log the failure and continue (accepting that the asset tracker's accounting may be stale, to be reconciled later).
2. **Alternatively**, wrap the call in a try/catch equivalent — check `failed` and emit an event or log rather than returning a fatal error.
3. **At minimum**, add a circuit-breaker: if `L2AssetTracker` is not deployed (empty account), the call already succeeds silently. Extend this to also succeed silently on revert, matching the behavior described in the comment: *"If no contract is deployed at L2AssetTracker, the call succeeds silently."*

The analog fix from the external report applies directly: use try/catch to skip revert callbacks rather than propagating them as fatal errors.

---

### Proof of Concept

1. Deploy ZKsync OS with `L2AssetTracker` in a state where `handleFinalizeBaseTokenBridgingOnL2` reverts for a specific `(fromChainId, amount)` pair (e.g., unregistered asset, or by upgrading the contract to add a revert condition).
2. Submit an L1→L2 transaction with `to_mint > 0` targeting any address.
3. `process_l1_transaction` calls `mint_base_token` → `notify_l2_asset_tracker` → `run_single_interaction` on `L2AssetTracker`.
4. `L2AssetTracker.handleFinalizeBaseTokenBridgingOnL2` reverts.
5. `notify_l2_asset_tracker` returns `Err(internal_error!("L2AssetTracker.handleFinalizeBaseTokenBridgingOnL2 reverted"))`.
6. `process_l1_transaction` propagates the error upward as a `BootloaderSubsystemError`.
7. Block processing halts — no transactions in the block can be finalized. [6](#0-5)

### Citations

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

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L855-915)
```rust
fn notify_l2_asset_tracker<'a, S: EthereumLikeTypes + 'a, Config: BasicBootloaderExecutionConfig>(
    system: &mut System<S>,
    system_functions: &mut HooksStorage<S, S::Allocator>,
    memories: RunnerMemoryBuffers<'a>,
    amount: U256,
    l1_chain_id: U256,
    resources: &mut S::Resources,
    tracer: &mut impl Tracer<S>,
    validator: &mut impl TxValidator<S>,
) -> Result<(), BootloaderSubsystemError>
where
    S::IO: IOSubsystemExt,
    S::Metadata: ZkSpecificPricingMetadata
        + BasicMetadata<S::IOTypes, TransactionMetadata = TxLevelMetadata<S::IOTypes>>,
{
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
    Ok(())
}
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L921-943)
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
```
