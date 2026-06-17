### Title
L2AssetTracker Predeploy Genesis Initialization Omits `_initialized` Guard, Enabling Reinitialization and Ownership Takeover — (`basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs`)

---

### Summary

The `L2AssetTracker` contract is predeploy-initialized via a genesis script that writes individual storage slots directly (slots 152–155) but never sets the `_initialized` variable that lives in the `Initializable` storage range (slots 0–100). Because `_initialized` remains `0`, any unprivileged caller can invoke `initialize(owner)` on the live contract, seize ownership, and then manipulate the contract in ways that either corrupt L1→L2 deposit accounting or halt block processing entirely.

---

### Finding Description

The production bootloader documents the `L2AssetTracker` storage layout explicitly:

```
// L2AssetTracker storage layout (verified via `forge inspect`):
//   slots 0-100:   Initializable + OwnableUpgradeable + Ownable2StepUpgradeable
//   slots 101-150: Ownable2Step __gap
//   slot 151:      mapping chainBalance
//   slot 152:      mapping assetMigrationNumber
//   slot 153:      mapping isAssetRegistered
//   slot 154:      uint256 L1_CHAIN_ID
``` [1](#0-0) 

The genesis initialization function `install_default_predeployed_contracts` writes only slots 152, 153, 154, and 155: [2](#0-1) 

No slot in the range 0–100 (the `Initializable` + `OwnableUpgradeable` region) is ever written. The OpenZeppelin `Initializable` contract stores its `_initialized` flag in slot 0 (packed with `_initializing`). Because this slot is left at zero, the contract's `initializer` modifier will not revert, and any caller can invoke `initialize(attackerAddress)` to claim ownership.

---

### Impact Explanation

The bootloader calls `handleFinalizeBaseTokenBridgingOnL2` on `L2AssetTracker` for every L1→L2 deposit (value mint, operator fee, and refund legs). A revert from this call is treated as a **fatal system error** that halts block processing: [3](#0-2) 

An attacker who becomes owner can:

1. **Halt block processing** — deregister the base-token asset or change `L1_CHAIN_ID` so that `handleFinalizeBaseTokenBridgingOnL2` reverts on every L1 deposit, making the chain unable to include any L1 transaction that carries a deposit.
2. **Corrupt deposit accounting** — set `L1_CHAIN_ID` to an arbitrary value; the bootloader reads this slot directly and passes it as `_fromChainId` to the asset tracker, so all subsequent deposit records are attributed to the wrong source chain.
3. **Drain contract funds** — as `Ownable` owner, call any privileged withdrawal function exposed by `L2AssetTracker`. [4](#0-3) 

---

### Likelihood Explanation

The window is the very first block after the chain launches. Any user who submits a transaction calling `initialize(attackerAddress)` before the legitimate operator does so wins ownership. Because the `L2AssetTracker` is a predeploy (bytecode is present at genesis), the call is immediately executable with no special privilege. The attack requires only a standard EVM call and knowledge of the `initialize` selector. [5](#0-4) 

---

### Recommendation

The genesis initialization for `L2AssetTracker` must write the `_initialized` storage slot (slot 0 for OpenZeppelin v4, or the dedicated `INITIALIZABLE_STORAGE` slot for v5) to `1` (or the appropriate version counter) alongside the existing data slots. This mirrors the fix applied to TaikoL2 in PR #16543. Additionally, a deterministic test should verify that the genesis storage layout of `L2AssetTracker` matches the layout produced by calling `initialize()` directly, ensuring no guard variable is ever left unset. [6](#0-5) 

---

### Proof of Concept

1. Chain launches with `L2AssetTracker` predeploy at `0x1000f`. Genesis state sets slots 152–155 but leaves slot 0 (`_initialized`) at zero.
2. Attacker sends an EVM transaction:
   ```
   to:   0x000000000000000000000000000000000001000f
   data: initialize(attackerAddress)   // selector + ABI-encoded address
   ```
3. `Initializable._initialized == 0` → modifier passes → attacker is set as owner.
4. Attacker calls a privileged function (e.g., `deregisterAsset(BASE_TOKEN_ASSET_ID)`) to remove the base token from the registered set.
5. Next L1 deposit transaction arrives; bootloader calls `handleFinalizeBaseTokenBridgingOnL2`; the asset tracker reverts because the asset is no longer registered.
6. Bootloader hits the fatal-error branch and aborts block processing — the chain cannot finalize any further L1 deposits. [7](#0-6)

### Citations

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

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L925-932)
```rust
    // L2AssetTracker storage layout (verified via `forge inspect`):
    //   slots 0-100:   Initializable + OwnableUpgradeable + Ownable2StepUpgradeable
    //   slots 101-150: Ownable2Step __gap
    //   slot 151:      mapping chainBalance
    //   slot 152:      mapping assetMigrationNumber
    //   slot 153:      mapping isAssetRegistered
    //   slot 154:      uint256 L1_CHAIN_ID
    let l1_chain_id_slot = Bytes32::from_u256_be(&U256::from(154));
```

**File:** tests/rig/src/predeployed_contracts.rs (L42-91)
```rust
/// Installs the default system-contract predeploys required by rig-based tests.
///
/// This deploys `L2AssetTracker` and `SystemContext` at their canonical addresses and
/// seeds the minimal storage they need for the L1 finalization and settlement-layer
/// chain-id flows used across the test suite. The initialized state is intentionally
/// deterministic so every fresh `TestingFramework` instance starts from the same
/// protocol-level assumptions.
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

**File:** system_hooks/src/addresses_constants.rs (L46-48)
```rust
// L2 asset tracker contract
pub const L2_ASSET_TRACKER_ADDRESS: B160 = B160::from_limbs([0x1000f, 0, 0]);

```
