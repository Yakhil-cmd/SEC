### Title
L2AssetTracker Revert Halts All L1→L2 Block Processing — (`basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs`)

---

### Summary

Every L1→L2 transaction with a non-zero deposit calls `notify_l2_asset_tracker`, which invokes `L2AssetTracker.handleFinalizeBaseTokenBridgingOnL2` on the predeployed contract at `0x1000f`. If that call reverts for any reason, the bootloader propagates the failure as a **fatal `BootloaderSubsystemError`** that halts block processing entirely — not just the individual transaction. This is the direct analog of the MINTR-pause issue: a critical, non-skippable operation depends on an external contract that can revert, and the failure path leaves the system stuck rather than degrading gracefully.

---

### Finding Description

`notify_l2_asset_tracker` is called up to three times per L1→L2 transaction — once for the value mint, once for the operator fee, and once for the refund: [1](#0-0) 

The function executes `handleFinalizeBaseTokenBridgingOnL2(fromChainId, amount)` against `L2_ASSET_TRACKER_ADDRESS` (`0x1000f`) using `with_infinite_ergs` (so gas exhaustion cannot mask the failure). If `asset_tracker_result.failed()` is `true`, the code immediately returns:

```rust
return Err(internal_error!(
    "L2AssetTracker.handleFinalizeBaseTokenBridgingOnL2 reverted"
).into());
``` [2](#0-1) 

This error propagates out of `mint_base_token` and then out of `process_l1_transaction` as a `BootloaderSubsystemError`, which the bootloader treats as a fatal block-level failure. The code comment explicitly acknowledges this design:

> "Failure halts block processing — if the asset tracker reverts, the chain's token accounting would be inconsistent, so we treat it as fatal rather than silently continuing with incorrect bookkeeping." [3](#0-2) 

`L2AssetTracker` is a real predeployed EVM contract (OwnableUpgradeable, with an `isAssetRegistered` mapping and `assetMigrationNumber` state) at address `0x1000f`: [4](#0-3) 

Its storage is seeded at genesis with specific values (`isAssetRegistered = 1`, `assetMigrationNumber = 1`, `L1_CHAIN_ID`). Any state inconsistency — or a governance-triggered pause — can cause `handleFinalizeBaseTokenBridgingOnL2` to revert. [5](#0-4) 

The three call sites in `process_l1_transaction` all propagate the error without any fallback: [6](#0-5) [7](#0-6) [8](#0-7) 

---

### Impact Explanation

If `L2AssetTracker.handleFinalizeBaseTokenBridgingOnL2` reverts for any reason, **every L1→L2 transaction with a non-zero deposit becomes a block-halting fatal error**. Unlike the original report (one AMO position stuck), here the entire block cannot be finalized. The sequencer cannot skip the failing transaction and continue — the block is dead. All pending L1→L2 deposits are frozen until the L2AssetTracker is repaired and a new block is attempted. This constitutes a complete denial of the L1→L2 bridge and a halt of state-transition processing.

---

### Likelihood Explanation

`L2AssetTracker` is an upgradeable contract (`OwnableUpgradeable`). A governance-triggered pause, a failed upgrade, or a state inconsistency (e.g., `isAssetRegistered` cleared, `assetMigrationNumber` reset, or `L1_CHAIN_ID` slot zeroed) would cause `handleFinalizeBaseTokenBridgingOnL2` to revert. The scenario is directly analogous to the original report: an emergency requiring the asset tracker to be paused (e.g., a bridge exploit) would simultaneously make it impossible to process any L1→L2 transaction, compounding the crisis. The `amount` parameter passed to the call is derived from the user-controlled `to_mint` field of the L1 transaction, meaning any user submitting an L1→L2 deposit triggers the call path.

---

### Recommendation

Mirror the fix from the original report: treat a revert from `L2AssetTracker` as a non-fatal condition. Instead of propagating the error as a block-halting fatal, the bootloader should either:

1. Log the failure and continue (accepting that the asset tracker's accounting may be stale), or
2. Fall back to a "no asset tracker" path (as already handled for the zero-amount case and for the pre-upgrade case where the address is empty).

Concretely, in `notify_l2_asset_tracker`, replace the fatal error return with a logged warning:

```rust
if failed {
    system_log!(
        system,
        "L2AssetTracker.handleFinalizeBaseTokenBridgingOnL2 failed for amount {amount:?} — continuing\n"
    );
    // Do not halt block processing; asset tracker accounting will be
    // reconciled separately.
}
``` [2](#0-1) 

---

### Proof of Concept

1. Deploy a modified `L2AssetTracker` at `0x1000f` that always reverts `handleFinalizeBaseTokenBridgingOnL2` (simulating a paused or broken contract).
2. Submit any L1→L2 priority transaction with `to_mint > 0` (e.g., `gas_limit * gas_price + 1`).
3. The bootloader calls `mint_base_token` → `notify_l2_asset_tracker` → `run_single_interaction` against `L2AssetTracker`.
4. `asset_tracker_result.failed()` returns `true`.
5. `notify_l2_asset_tracker` returns `Err(internal_error!("L2AssetTracker.handleFinalizeBaseTokenBridgingOnL2 reverted"))`.
6. `process_l1_transaction` propagates this as a `BootloaderSubsystemError`.
7. Block processing halts; no further transactions in the block are processed.

The existing test infrastructure confirms the call path is live and exercised on every deposit: [9](#0-8)

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

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L338-359)
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
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L631-657)
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
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L848-912)
```rust
/// Failure halts block processing — if the asset tracker reverts, the
/// chain's token accounting would be inconsistent, so we treat it as
/// fatal rather than silently continuing with incorrect bookkeeping.
///
/// If no contract is deployed at L2AssetTracker, the call succeeds silently
/// (a call to an empty address returns success with no returndata in EVM).
/// However, we are certain that L2AssetTracker is available after the upgrade.
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
```

**File:** system_hooks/src/addresses_constants.rs (L46-47)
```rust
// L2 asset tracker contract
pub const L2_ASSET_TRACKER_ADDRESS: B160 = B160::from_limbs([0x1000f, 0, 0]);
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

**File:** tests/instances/transactions/src/asset_tracker.rs (L99-144)
```rust
/// Verify that when an L1 tx has a deposit (total_deposited > 0), the bootloader
/// notifies the real `L2AssetTracker` and it records the deposited amount.
#[test]
fn test_asset_tracker_called_on_deposit() {
    let from = address!("1234000000000000000000000000000000000000");
    let to = address!("abcd000000000000000000000000000000000000");
    let gas_price: u128 = 10_000;
    let gas_limit: u128 = 50_000;
    let value = rig::alloy::primitives::U256::from(500);
    let to_mint = rig::alloy::primitives::U256::from(gas_limit * gas_price)
        + rig::alloy::primitives::U256::from(1_000_000u64);

    let mut tester = TestingFramework::new().with_balance(from, U256::from(u64::MAX));

    let tx: ZKsyncTxEnvelope = L1TxBuilder::new()
        .from(from)
        .to(to)
        .gas_price(gas_price)
        .gas_limit(gas_limit)
        .value(value)
        .to_mint(to_mint)
        .build();

    let output = tester.execute_block(vec![tx]);

    assert_eq!(output.tx_results.len(), 1);
    let tx_result = output.tx_results[0].as_ref().expect("tx should not error");
    assert!(tx_result.is_success(), "L1 tx should succeed");

    let accumulated = read_total_successful_deposits_from_l1(&mut tester);
    assert_eq!(
        accumulated,
        U256::from_be_slice(&to_mint.to_be_bytes::<32>()),
        "recorded deposits from L1 should equal to_mint"
    );

    // computational_native_used reflects the main tx body computation
    // plus intrinsic native. Post-execution operations (asset tracker
    // notifications, coinbase transfer, refund) run on FORMAL_INFINITE
    // and their cost is covered by L1_TX_INTRINSIC_NATIVE_COST, not
    // measured at runtime.
    assert!(
        tx_result.computational_native_used > 0,
        "computational_native_used should be nonzero"
    );
}
```
