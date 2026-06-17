### Title
L1 Transaction Intrinsic Pubdata Undercounts Asset Tracker Calls: Only One `ASSET_TRACKER_INTRINSIC_PUBDATA` Budgeted for Up to Three `notify_l2_asset_tracker` Invocations - (`File: basic_bootloader/src/bootloader/constants.rs`)

### Summary

`L1_TX_INTRINSIC_PUBDATA` in `basic_bootloader/src/bootloader/constants.rs` includes only a single `ASSET_TRACKER_INTRINSIC_PUBDATA` (65 bytes) for the `L2AssetTracker.handleFinalizeBaseTokenBridgingOnL2` storage diff. However, `process_l1_transaction` in `basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs` calls `mint_base_token` (which internally calls `notify_l2_asset_tracker`) **up to three times** per L1 transaction — once for the value mint to the sender, once for the operator fee payment to coinbase, and once for the refund to the refund recipient. Each call produces its own `SSTORE` diff in `L2AssetTracker.interopInfo[assetId].totalSuccessfulDepositsFromL1`. The intrinsic pubdata budget only accounts for one of these three diffs, causing the actual pubdata consumed to exceed the pre-charged intrinsic amount by up to `2 × ASSET_TRACKER_INTRINSIC_PUBDATA = 130 bytes`.

### Finding Description

The `L1_TX_INTRINSIC_PUBDATA` constant is defined as:

```
L1_TX_INTRINSIC_PUBDATA = 88
    + COINBASE_BALANCE_INTRINSIC_PUBDATA   // 66
    + TREASURY_BALANCE_INTRINSIC_PUBDATA   // 66
    + REFUND_RECIPIENT_BALANCE_INTRINSIC_PUBDATA // 66
    + ASSET_TRACKER_INTRINSIC_PUBDATA;     // 65  ← only ONE
``` [1](#0-0) 

The comment on `ASSET_TRACKER_INTRINSIC_PUBDATA` explicitly describes it as covering only the **value-mint notification** call:

> "Pubdata produced by the L2AssetTracker.handleFinalizeBaseTokenBridgingOnL2 call that the bootloader makes **inside the L1 tx execution frame (value-mint notification)**." [2](#0-1) 

However, `process_l1_transaction` calls `mint_base_token` (which calls `notify_l2_asset_tracker`) **three separate times**:

1. **Value mint** — inside `execute_l1_transaction_and_notify_result`, transferring `to_transfer` to `from`: [3](#0-2) 

2. **Operator fee** — after execution, minting `pay_to_operator` to `coinbase`: [4](#0-3) 

3. **Refund** — minting `to_refund_recipient` to the refund recipient: [5](#0-4) 

Each call to `notify_l2_asset_tracker` executes `handleFinalizeBaseTokenBridgingOnL2` on `L2AssetTracker`, which performs an `SSTORE` to `interopInfo[assetId].totalSuccessfulDepositsFromL1 += _amount`. Each such `SSTORE` produces a storage diff of `ASSET_TRACKER_INTRINSIC_PUBDATA = 65 bytes`. The test file confirms all three calls happen per deposit: [6](#0-5) 

The intrinsic pubdata budget pre-charges only `1 × 65 = 65 bytes` for the asset tracker, but the actual worst-case is `3 × 65 = 195 bytes` — an undercount of **130 bytes**.

The `L1_TX_INTRINSIC_NATIVE_COST` comment also only accounts for the coinbase notification and the refund notification (warm-path), but the value-mint notification is charged against user resources inside the execution frame. The pubdata for all three asset tracker calls, however, is supposed to be covered by `L1_TX_INTRINSIC_PUBDATA`. [7](#0-6) 

### Impact Explanation

The intrinsic pubdata is pre-charged from the user's native resource budget before execution begins. If the actual pubdata consumed exceeds the pre-charged amount, the system either:

1. **Incorrectly charges the user less pubdata than was actually consumed**, allowing L1→L2 transactions to consume more DA bandwidth than they paid for. This is a resource accounting bug analogous to the original report's "oracle sends less than expected price" — here the system charges less pubdata cost than the actual state diff produced.

2. In the worst case (all three asset tracker calls fire with non-zero amounts), the pubdata undercount is `2 × 65 = 130 bytes` per L1 transaction. At scale, an attacker can craft L1→L2 transactions with non-zero value, non-zero gas price, and a non-zero refund recipient to maximize the three-call scenario, systematically underpaying for DA.

**Impact: Medium** — resource accounting mismatch causing underpayment for pubdata on L1→L2 transactions. Does not directly steal funds but breaks the economic invariant that each transaction pays for its full DA footprint.

### Likelihood Explanation

Every L1→L2 priority transaction with `total_deposited > 0`, a non-zero gas price, and a non-zero refund recipient will trigger all three `notify_l2_asset_tracker` calls. This is the standard deposit flow. Any unprivileged user submitting an L1→L2 deposit transaction triggers this path.

**Likelihood: High** — the standard L1→L2 deposit path with value + fee + refund recipient is the common case.

### Recommendation

Update `L1_TX_INTRINSIC_PUBDATA` to account for all three potential asset tracker storage diffs:

```rust
pub const L1_TX_INTRINSIC_PUBDATA: u64 = 88
    + COINBASE_BALANCE_INTRINSIC_PUBDATA
    + TREASURY_BALANCE_INTRINSIC_PUBDATA
    + REFUND_RECIPIENT_BALANCE_INTRINSIC_PUBDATA
    + 3 * ASSET_TRACKER_INTRINSIC_PUBDATA;  // one per: value-mint, operator-fee, refund
``` [1](#0-0) 

Also update the comment on `ASSET_TRACKER_INTRINSIC_PUBDATA` to clarify it is a per-call unit, and that up to three calls can occur per L1 transaction.

### Proof of Concept

1. Submit an L1→L2 priority transaction with:
   - `total_deposited = gas_limit * gas_price + value` (non-zero value)
   - `gas_price > 0` (non-zero operator fee)
   - `reserved[1]` (refund recipient) set to a non-zero address

2. The bootloader calls `mint_base_token` three times:
   - Call 1 (value mint): `notify_l2_asset_tracker(value)` → SSTORE in L2AssetTracker (+65 bytes pubdata)
   - Call 2 (operator fee): `notify_l2_asset_tracker(pay_to_operator)` → SSTORE in L2AssetTracker (+65 bytes pubdata)
   - Call 3 (refund): `notify_l2_asset_tracker(to_refund_recipient)` → SSTORE in L2AssetTracker (+65 bytes pubdata)

3. Total asset tracker pubdata = 195 bytes. Pre-charged intrinsic = 65 bytes. Deficit = 130 bytes.

4. The transaction pays for only 1/3 of the actual L2AssetTracker DA cost, with the remaining 130 bytes of pubdata consumed without corresponding payment. [8](#0-7) [9](#0-8) [10](#0-9)

### Citations

**File:** basic_bootloader/src/bootloader/constants.rs (L147-195)
```rust
// Covers intrinsic L1 tx work not charged as tx-body computation.
//
//  - storing and hashing the L1 tx log:
//      EVENT_STORAGE_BASE_NATIVE_COST
//    + keccak256_native_cost(88)
//    + 2 * keccak256_native_cost(64)
//    = 6_000 + 20_000 + 40_000
//    = 66_000
//  - hashing tx hash into the rolling hash and linear hashers:
//      3 * keccak256_native_cost(64)
//    = 3 * 20_000
//    = 60_000
//  - coinbase transfer:
//      warm existing balance write
//    = WARM_STORAGE_READ_NATIVE_COST + WARM_STORAGE_WRITE_EXTRA_NATIVE_COST x 2 (to account for treasury)
//    = (4_000 + 1_000) x 2
//    = 10_000
//  - coinbase L2AssetTracker notification:
//      cold call into L2AssetTracker
//    + BASE_TOKEN_ASSET_ID read
//    + isAssetRegistered read
//    + assetMigrationNumber read
//    + L2BaseTokenZKOS.totalSupply() path
//    + L2_CHAIN_ASSET_HANDLER.migrationNumber() call
//    + assetMigrationNumber write
//    + SystemContext.currentSettlementLayerChainId() call
//    + interopInfo.totalSuccessfulDepositsFromL1 += amount
//    = 132_600
//    + 125_120
//    + 145_120
//    + 286_240
//    + 392_340
//    + 277_720
//    + 164_800
//    + 257_720
//    + 391_040
//    ~= 2_172_700
//  - refund transfer:
//      treasury cold existing write
//    + refund recipient cold new write
//    = 171_680 + 363_040
//    = 534_720
//  - refund L2AssetTracker notification:
//      warm-path estimate
//    = 32_000
//
// We use the cold-path cost for asset tracker first notification because
// first mint / call to L2AssetTracker can fail due to out-of-native
pub const L1_TX_INTRINSIC_NATIVE_COST: u64 = 2_875_420;
```

**File:** basic_bootloader/src/bootloader/constants.rs (L238-246)
```rust
// Pubdata produced by the L2AssetTracker.handleFinalizeBaseTokenBridgingOnL2
// call that the bootloader makes inside the L1 tx execution frame (value-mint
// notification). In the steady-state case (base token already registered,
// settled on L1), the contract performs a single SSTORE:
//   interopInfo[assetId].totalSuccessfulDepositsFromL1 += _amount
// Each storage diff is encoded as 32 bytes (derived key) + compressed value
// diff. The worst-case compressed value using the Add strategy with a
// 256-bit amount falls back to Nothing encoding = 33 bytes.
pub const ASSET_TRACKER_INTRINSIC_PUBDATA: u64 = 32 + 33;
```

**File:** basic_bootloader/src/bootloader/constants.rs (L248-254)
```rust
// Needed to publish the L1 tx log, coinbase balance, treasury balance, refund
// recipient balance, and asset tracker state diff.
pub const L1_TX_INTRINSIC_PUBDATA: u64 = 88
    + COINBASE_BALANCE_INTRINSIC_PUBDATA
    + TREASURY_BALANCE_INTRINSIC_PUBDATA
    + REFUND_RECIPIENT_BALANCE_INTRINSIC_PUBDATA
    + ASSET_TRACKER_INTRINSIC_PUBDATA;
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L288-360)
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

    // Refund
    let to_refund_recipient = if !is_success {
        // Upgrade transactions must always succeed
        if !is_priority_op {
            return Err(internal_error!("Upgrade transaction must succeed").into());
        }
        // If the transaction reverts, then the minting of the deposit
        // reverted too. Thus, we need to refund the entire deposit minus
        // the fee (`pay_to_operator`).
        total_deposited
            .checked_sub(pay_to_operator)
            .ok_or(internal_error!("td-pto"))
    } else {
        // If the transaction succeeds, then it is assumed that the
        // mint to `from` address was transferred correctly too.
        // In this case, we just refund the unused gas that the
        // transaction paid for initially.
        let prepaid_fee = gas_price
            .checked_mul(U256::from(transaction.gas_limit.read()))
            .ok_or(internal_error!("gp*gl"))?;
        prepaid_fee
            .checked_sub(pay_to_operator)
            .ok_or(internal_error!("pf-pto"))
    }?;
    // Mint refund portion of the deposit to the refund recipient.
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

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L631-645)
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

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L855-914)
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
