### Title
`L2AssetTracker` Revert Causes Fatal Chain Halt on Every L1→L2 Deposit — (`basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs`)

---

### Summary

The bootloader unconditionally treats any EVM revert from `L2AssetTracker.handleFinalizeBaseTokenBridgingOnL2` as a fatal internal error that halts all block processing. Because this call is made for every L1→L2 transaction with a non-zero deposit (`to_mint > 0`), any condition that causes `L2AssetTracker` to revert — including the contract being paused, upgraded to a broken version, or having inconsistent internal state — will permanently halt the chain's ability to process L1→L2 deposits.

---

### Finding Description

In `notify_l2_asset_tracker`, the bootloader calls `handleFinalizeBaseTokenBridgingOnL2(uint256,uint256)` on the `L2AssetTracker` EVM contract (an upgradeable `OwnableUpgradeable` / `Ownable2StepUpgradeable` contract). If the call reverts, the bootloader immediately returns a fatal `internal_error!`, propagating up through `mint_base_token` → `execute_l1_transaction_and_notify_result` → `process_l1_transaction`, halting block processing entirely:

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

This call is made up to three times per L1→L2 transaction: once for the value mint, once for the operator fee, and once for the refund. [2](#0-1) 

The `L2AssetTracker` is an EVM contract (not a system hook) deployed at a system address. Its `handleFinalizeBaseTokenBridgingOnL2` function contains internal guards (e.g., `isAssetRegistered`, `assetMigrationNumber` checks) and the contract is upgradeable. If the contract is paused (via a `Pausable` mechanism added in an upgrade), if an upgrade introduces a revert path, or if internal state becomes inconsistent, every L1→L2 deposit will trigger a fatal chain halt.

The `L2AssetTracker` source is referenced in the code: [3](#0-2) 

Its storage layout (slots 152–154) is read directly by the bootloader, confirming it is a live EVM contract with mutable state: [4](#0-3) 

The predeploy setup confirms the contract is initialized with `isAssetRegistered`, `assetMigrationNumber`, and `L1_CHAIN_ID` storage slots that must be in a specific state for the call to succeed: [5](#0-4) 

---

### Impact Explanation

If `L2AssetTracker.handleFinalizeBaseTokenBridgingOnL2` reverts for any reason:

- Every L1→L2 transaction with `to_mint > 0` causes `process_l1_transaction` to return a fatal error.
- Block processing halts entirely — the chain cannot finalize any block containing such a transaction.
- Since L1→L2 transactions are submitted from L1 and cannot be skipped (they are priority queue entries), the chain is effectively bricked for all deposit operations.
- This is a **chain-level DoS** with no in-protocol recovery path.

The code comment itself acknowledges the severity: *"block processing cannot continue."* [6](#0-5) 

---

### Likelihood Explanation

The `L2AssetTracker` is an upgradeable EVM contract. Realistic revert scenarios include:

1. **Governance pause**: If a `Pausable` mechanism is added in an upgrade (common for bridge contracts), a governance pause during maintenance halts the chain.
2. **Upgrade bug**: A broken upgrade to `L2AssetTracker` that causes `handleFinalizeBaseTokenBridgingOnL2` to revert under any condition halts the chain.
3. **Internal state inconsistency**: If `isAssetRegistered[assetId]` is cleared or `assetMigrationNumber` is reset (e.g., via a storage-corrupting upgrade), the function reverts.
4. **Unprivileged trigger**: Any user who submits an L1→L2 transaction with `to_mint > 0` on L1 triggers this code path. The attacker does not need to control `L2AssetTracker` — they only need to submit a deposit while the contract is in a reverting state.

The entry path is fully unprivileged: L1→L2 transactions are submitted by any user on L1. [7](#0-6) 

---

### Recommendation

1. **Add a pre-call liveness check**: Before calling `handleFinalizeBaseTokenBridgingOnL2`, check whether `L2AssetTracker` is paused (if a `Pausable` interface is present) and handle the paused case gracefully rather than fatally.

2. **Degrade gracefully on revert**: Instead of treating `L2AssetTracker` revert as a fatal chain halt, consider logging the failure and continuing block processing with a fallback accounting path. The current design creates a single point of failure for all L1→L2 deposits.

3. **Analog to the original recommendation**: Just as the original report recommended `require(!stETH.isStopped(), ...)` before calling the pausable contract, ZKsync OS should add:
   ```rust
   // Before calling notify_l2_asset_tracker:
   if l2_asset_tracker_is_paused(system) {
       // log and skip, or use fallback accounting
   }
   ```

4. **Isolate the fatal path**: If the accounting consistency requirement is non-negotiable, at minimum ensure the `L2AssetTracker` contract itself cannot be paused or upgraded to a reverting state while L1→L2 deposits are active.

---

### Proof of Concept

1. Deploy `L2AssetTracker` in a state where `handleFinalizeBaseTokenBridgingOnL2` reverts (e.g., by upgrading it to a version with a `whenNotPaused` modifier and calling `pause()`).
2. Submit any L1→L2 transaction with `to_mint > 0` from an unprivileged L1 address.
3. The bootloader calls `mint_base_token` → `notify_l2_asset_tracker` → `run_single_interaction` targeting `L2AssetTracker`.
4. `L2AssetTracker` reverts; `asset_tracker_result.failed()` returns `true`.
5. `notify_l2_asset_tracker` returns `Err(internal_error!("L2AssetTracker.handleFinalizeBaseTokenBridgingOnL2 reverted"))`.
6. This propagates as a fatal error through `execute_l1_transaction_and_notify_result` and `process_l1_transaction`.
7. Block processing halts; no further transactions in the block are processed. [8](#0-7) [9](#0-8)

### Citations

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L733-740)
```rust
/// Notifies L2AssetTracker and transfers base tokens from the treasury
/// to [to] in a single operation.
///
/// This function replicates the behaviour of the corresponding call from bootloader to era contracts:
/// https://github.com/matter-labs/era-contracts/blob/2f024c5764e7a873ce1dda5fb990331559996441/l1-contracts/contracts/l2-system/era/L2BaseTokenEra.sol#L86
///
/// Notify the asset tracker BEFORE changing balances/totalSupply, so that
/// _needToForceSetAssetMigrationOnL2 can use totalSupply() == 0 consistently.
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L741-769)
```rust
fn mint_base_token<'a, S: EthereumLikeTypes + 'a, Config: BasicBootloaderExecutionConfig>(
    system: &mut System<S>,
    system_functions: &mut HooksStorage<S, S::Allocator>,
    memories: RunnerMemoryBuffers<'a>,
    amount: &U256,
    to: &B160,
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

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L836-855)
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
fn notify_l2_asset_tracker<'a, S: EthereumLikeTypes + 'a, Config: BasicBootloaderExecutionConfig>(
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

**File:** tests/rig/src/predeployed_contracts.rs (L49-81)
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
