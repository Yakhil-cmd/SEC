### Title
Placeholder `L1_TX_NATIVE_PRICE` Constant Causes Incorrect Native Resource Accounting for L1→L2 Transactions - (File: `basic_bootloader/src/bootloader/constants.rs`)

### Summary
`L1_TX_NATIVE_PRICE` is set to the arbitrary placeholder value `10` with an explicit `TODO` comment acknowledging it has not been properly calibrated. This constant governs how many native (proving) resources an L1→L2 priority transaction is allocated per gas unit. Because the value is a staging placeholder rather than a production-calibrated figure, every L1→L2 priority transaction with a non-zero `gas_price` receives a native resource budget that is disconnected from actual proving costs.

### Finding Description
In `basic_bootloader/src/bootloader/constants.rs`, the constant used to price native resources for L1→L2 transactions is explicitly marked as unfinished:

```rust
// Default native price for L1->L2 transactions.
// TODO (EVM-1157): find a reasonable value for it.
pub const L1_TX_NATIVE_PRICE: U256 = U256::from_limbs([10, 0, 0, 0]);
``` [1](#0-0) 

This constant is consumed in `process_l1_transaction.rs` to derive `native_per_gas` for every L1→L2 priority transaction that carries a non-zero `gas_price`:

```rust
let native_price = L1_TX_NATIVE_PRICE;          // == 10
let native_per_gas = ...
    u256_try_to_u64(&gas_price.div_ceil(native_price))  // gas_price / 10
``` [2](#0-1) 

`native_per_gas` is then multiplied by `gas_limit` to produce `native_prepaid_from_gas`, which becomes the transaction's native resource budget:

```rust
let native_prepaid_from_gas = native_per_gas.checked_mul(gas_limit)
    .unwrap_or_else(|| { ...; u64::MAX });
``` [3](#0-2) 

For L2 transactions the analogous `native_price` is fetched from the oracle/operator metadata and reflects actual proving costs. For L1 transactions it is hardcoded to `10` — a value the codebase itself admits is not yet determined.

### Impact Explanation
Because `L1_TX_NATIVE_PRICE = 10` is far below any realistic proving-cost calibration, `native_per_gas = gas_price / 10` is inflated by an unknown factor relative to the correct value. This produces two concrete effects:

1. **Over-allocation of native resources per L1 tx.** A priority transaction with `gas_price = 1000` and `gas_limit = 72_000_000` (the expected upper bound for L1 txs) yields `native_per_gas = 100` and `native_prepaid = 7.2 × 10⁹`, which exceeds `MAX_NATIVE_COMPUTATIONAL = 2³⁵ ≈ 3.4 × 10¹⁰`. With a slightly higher gas price the product overflows `u64` and saturates to `u64::MAX`, giving the transaction an effectively unlimited native budget within the block.

2. **Block native resource exhaustion.** A single L1→L2 transaction with an inflated native budget can consume the entire block's `MAX_NATIVE_COMPUTATIONAL` allowance, starving subsequent L2 transactions and degrading throughput — without paying a proportionate fee for the proving work consumed. [4](#0-3) 

### Likelihood Explanation
L1→L2 priority transactions are submitted permissionlessly from Ethereum L1. Any unprivileged user can craft a transaction with an arbitrary `gas_price` and `gas_limit`. No privileged role or external oracle manipulation is required. The attacker only needs to submit a standard L1→L2 deposit or call transaction with a sufficiently high gas price to trigger the overflow path or to obtain a disproportionately large native resource budget.

### Recommendation
Replace the placeholder with a properly calibrated value derived from the same proving-cost model used for L2 transactions, or derive it dynamically from the oracle-supplied `native_price` at block-processing time (as is done for L2 transactions in `validation_impl.rs`). At minimum, resolve `TODO (EVM-1157)` before deploying to production and add an assertion or compile-time check that `L1_TX_NATIVE_PRICE` is non-trivially small.

### Proof of Concept
1. Attacker submits an L1→L2 priority transaction with `gas_price = 1_000_000` and `gas_limit = 72_000_000`.
2. `native_per_gas = 1_000_000 / 10 = 100_000`.
3. `native_prepaid_from_gas = 100_000 × 72_000_000 = 7.2 × 10¹²`, which overflows `u64::MAX ≈ 1.8 × 10¹⁹` — the `checked_mul` saturates to `u64::MAX`.
4. The transaction is allocated `u64::MAX` native resources, capped only by `MAX_NATIVE_COMPUTATIONAL = 2³⁵` at the block level.
5. The transaction can consume the entire block's native resource budget, blocking all subsequent L2 transactions in that block from being included, without paying a fee proportional to the proving work performed. [3](#0-2) [5](#0-4)

### Citations

**File:** basic_bootloader/src/bootloader/constants.rs (L64-66)
```rust
// Default native price for L1->L2 transactions.
// TODO (EVM-1157): find a reasonable value for it.
pub const L1_TX_NATIVE_PRICE: U256 = U256::from_limbs([10, 0, 0, 0]);
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L453-475)
```rust
    // For L1->L2 txs, we use a constant native price to avoid censorship.
    let native_price = L1_TX_NATIVE_PRICE;
    let native_per_gas = if is_priority_op {
        if gas_price.is_zero() {
            if Config::SIMULATION {
                u256_try_to_u64(&system.get_eip1559_basefee().div_ceil(native_price))
                    .unwrap_or_else(|| {
                        system_log!(
                            system,
                            "Native per gas calculation for L1 tx overflows, using saturated arithmetic instead");
                        u64::MAX
                    })
            } else {
                FREE_L1_TX_NATIVE_PER_GAS
            }
        } else {
            u256_try_to_u64(&gas_price.div_ceil(native_price)).unwrap_or_else(|| {
                system_log!(
                    system,
                    "Native per gas calculation for L1 tx overflows, using saturated arithmetic instead");
                u64::MAX
            })
        }
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L490-496)
```rust
    let native_prepaid_from_gas = native_per_gas.checked_mul(gas_limit)
        .unwrap_or_else(|| {
            system_log!(
                system,
                "Native prepaid from gas calculation for L1 tx overflows, using saturated arithmetic instead");
                u64::MAX
        });
```

**File:** zk_ee/src/system/constants.rs (L26-26)
```rust
pub const MAX_NATIVE_COMPUTATIONAL: u64 = 1 << 35;
```

**File:** basic_bootloader/src/bootloader/block_flow/zk/mod.rs (L68-76)
```rust
    } else if !cfg!(feature = "resources_for_tester")
        && computational_native_used > MAX_NATIVE_COMPUTATIONAL
    {
        // ZKsync OS-specific resources are not checked for evm tester
        system_log!(
            system,
            "Block native limit reached, invalidating transaction\n"
        );
        Err(InvalidTransaction::BlockNativeLimitReached)
```
