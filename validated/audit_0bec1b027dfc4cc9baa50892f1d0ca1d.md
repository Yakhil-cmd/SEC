### Title
Hardcoded `L1_TX_INTRINSIC_NATIVE_COST` Can Diverge from Actual `L2AssetTracker` Execution Cost, Causing All L1→L2 Value Deposits to Always Revert - (File: `basic_bootloader/src/bootloader/constants.rs`)

---

### Summary

`L1_TX_INTRINSIC_NATIVE_COST` is a compile-time constant (`2_875_420`) that pre-charges native resources from the user's gas budget to cover the cost of post-execution operations during L1→L2 transaction processing — most critically the cold-path call to `L2AssetTracker.handleFinalizeBaseTokenBridgingOnL2`. This constant is derived from a specific snapshot of the `L2AssetTracker` contract's internal execution path. If the deployed `L2AssetTracker` contract diverges from that snapshot (e.g., via a protocol upgrade), the first `notify_l2_asset_tracker` call — which runs against the user's native resources — can exhaust the budget, causing every L1→L2 transaction that carries a non-zero value deposit to revert at the block level.

---

### Finding Description

`L1_TX_INTRINSIC_NATIVE_COST` is defined as a single magic number:

```rust
pub const L1_TX_INTRINSIC_NATIVE_COST: u64 = 2_875_420;
``` [1](#0-0) 

The constant is built from a hand-counted breakdown of the `L2AssetTracker` contract's execution path, including specific storage reads (`BASE_TOKEN_ASSET_ID`, `isAssetRegistered`, `assetMigrationNumber`), external calls (`L2BaseTokenZKOS.totalSupply()`, `L2_CHAIN_ASSET_HANDLER.migrationNumber()`), and a final SSTORE (`interopInfo.totalSuccessfulDepositsFromL1 += amount`): [2](#0-1) 

This constant is deducted from the user's native budget in `calculate_l1_tx_intrinsic_computational_native_resources` before the transaction body runs: [3](#0-2) 

During L1→L2 transaction execution, `notify_l2_asset_tracker` is called **up to three times** — once for the value mint (inside the main execution frame, against user resources), once for the operator fee, and once for the refund: [4](#0-3) 

The critical distinction is that the **value-mint** call runs against the user's native resources (not `FORMAL_INFINITE`), while the operator-fee and refund calls use `FORMAL_INFINITE`: [5](#0-4) 

The code comment explicitly acknowledges this risk:

> "We use the cold-path cost for asset tracker first notification because first mint / call to L2AssetTracker can fail due to out-of-native" [1](#0-0) 

If the `L2AssetTracker` contract is upgraded and its actual native cost exceeds `L1_TX_INTRINSIC_NATIVE_COST`, the value-mint `notify_l2_asset_tracker` call exhausts the user's native budget. The out-of-native error is caught and treated as a revert:

<cite repo="Jaredbentat/

### Citations

**File:** basic_bootloader/src/bootloader/constants.rs (L146-195)
```rust
/// Constant part of l1 tx intrinsic computational native cost.
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

**File:** basic_bootloader/src/bootloader/transaction_flow/gas_helpers.rs (L241-251)
```rust
pub fn calculate_l1_tx_intrinsic_computational_native_resources(calldata_byte_length: u64) -> u64 {
    let mut intrinsic_computational_native_resources = L1_TX_INTRINSIC_NATIVE_COST;

    intrinsic_computational_native_resources = intrinsic_computational_native_resources
        .saturating_add(
            calldata_byte_length
                .saturating_mul(L1_TX_INTRINSIC_COMPUTATIONAL_NATIVE_PER_CALLDATA_BYTE),
        );

    intrinsic_computational_native_resources
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
