### Title
Hardcoded `L1_TX_NATIVE_PRICE` Causes Stale Native Resource Accounting for L1→L2 Transactions - (`basic_bootloader/src/bootloader/constants.rs`)

### Summary
`L1_TX_NATIVE_PRICE` is a compile-time constant used to derive the native resource limit for all L1→L2 (priority) transactions. Unlike L2 transactions — which use an operator-configurable `nativePrice` from `system.get_native_price()` — L1→L2 transactions use this hardcoded value with no on-chain setter. If the actual proving cost changes (prover upgrade, hardware change, ZKsync OS update), the native resource limit for every L1→L2 transaction will be systematically wrong, with no way to correct it short of a code upgrade.

### Finding Description

`L1_TX_NATIVE_PRICE` is defined as a compile-time constant:

```rust
// TODO (EVM-1157): find a reasonable value for it.
pub const L1_TX_NATIVE_PRICE: U256 = U256::from_limbs([10, 0, 0, 0]);
``` [1](#0-0) 

This constant is used in `process_l1_transaction.rs` as the denominator to compute `native_per_gas` for priority L1→L2 transactions:

```rust
let native_price = L1_TX_NATIVE_PRICE;
let native_per_gas = if is_priority_op {
    if gas_price.is_zero() { ... } else {
        u256_try_to_u64(&gas_price.div_ceil(native_price))...
    }
} else {
    FREE_L1_TX_NATIVE_PER_GAS
};
``` [2](#0-1) 

`native_per_gas` then directly sets the native resource limit for the transaction:

```rust
let native_prepaid_from_gas = native_per_gas.checked_mul(gas_limit)...;
``` [3](#0-2) 

By contrast, L2 transactions obtain the native price dynamically from the system:

```rust
let native_price = system.get_native_price();
``` [4](#0-3) 

The asymmetry is structural: the operator can update the L2 native price at any time, but the L1 native price is baked into the binary. The codebase itself acknowledges the value is a placeholder (`TODO (EVM-1157): find a reasonable value for it.`).

The native resource models the off-chain proving cost ("how many RISC-V cycles it takes to prove a given computation"): [5](#0-4) 

If a transaction runs out of native resources, the entire transaction is reverted: [6](#0-5) 

### Impact Explanation

Two failure modes exist depending on the direction of drift:

**Under-charging (L1_TX_NATIVE_PRICE too low relative to actual proving cost):** `native_per_gas` is computed as `gas_price / L1_TX_NATIVE_PRICE`. If `L1_TX_NATIVE_PRICE` is too low, `native_per_gas` is too high, and the native limit granted to L1→L2 transactions is too large. Users pay the same ETH fee but receive more proving budget than they paid for. An attacker can craft L1→L2 transactions with computationally expensive calldata or execution to consume proving resources far beyond what was economically justified, degrading block proving throughput and imposing uncompensated costs on the protocol.

**Over-charging (L1_TX_NATIVE_PRICE too high relative to actual proving cost):** `native_per_gas` is too low, the native limit is too small, and legitimate L1→L2 transactions revert with out-of-native-resources errors even though they have sufficient EVM gas. This is a DoS on the L1→L2 bridge for all users.

### Likelihood Explanation

Medium. ZKsync OS is a ZK rollup whose proving cost is directly tied to the RISC-V prover implementation. Prover upgrades, hardware changes, or changes to the proving circuit are expected over the protocol's lifetime. The TODO comment in the code confirms the current value is provisional. Because L1→L2 transactions are initiated on L1 (Ethereum) by unprivileged users, any drift in `L1_TX_NATIVE_PRICE` is immediately exploitable by any transaction sender without any privileged access.

### Recommendation

Replace the compile-time constant `L1_TX_NATIVE_PRICE` with an operator-configurable value stored in the system metadata (analogous to how `nativePrice` is already handled for L2 transactions via `system.get_native_price()`). Add an initialization path and a privileged setter so the operator can update the L1 native price when proving costs change, without requiring a full code upgrade.

### Proof of Concept

1. The prover is upgraded and actual proving cost per RISC-V cycle increases by 10×.
2. The operator updates the L2 native price via `system.get_native_price()` to reflect the new cost.
3. L2 transactions are now correctly charged.
4. However, `L1_TX_NATIVE_PRICE` remains `10` (hardcoded). For an L1→L2 transaction with `gas_price = 100` and `gas_limit = 1_000_000`:
   - `native_per_gas = 100 / 10 = 10`
   - `native_limit = 10 * 1_000_000 = 10_000_000`
   - Actual fair native limit at new cost should be `100 / 100 = 1`, giving `native_limit = 1_000_000`
5. The attacker's L1→L2 transaction receives 10× more native (proving) budget than it paid for.
6. The attacker crafts a transaction with expensive computation (e.g., large calldata triggering many keccak rounds, or deep call stacks) that consumes the full inflated native budget.
7. The block prover must process 10× more proving work than the fee collected, imposing uncompensated cost on the protocol.

The root cause is exclusively in `basic_bootloader/src/bootloader/constants.rs` line 66 (`L1_TX_NATIVE_PRICE`) and its consumption in `basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs` lines 454–479, with no privileged-access requirement on the attacker's side.

### Citations

**File:** basic_bootloader/src/bootloader/constants.rs (L64-66)
```rust
// Default native price for L1->L2 transactions.
// TODO (EVM-1157): find a reasonable value for it.
pub const L1_TX_NATIVE_PRICE: U256 = U256::from_limbs([10, 0, 0, 0]);
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L453-479)
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
    } else {
        // Upgrade txs are paid by the protocol, so we use a fixed native per gas
        FREE_L1_TX_NATIVE_PER_GAS
    };
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

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs (L107-107)
```rust
    let native_price = system.get_native_price();
```

**File:** docs/double_resource_accounting.md (L17-17)
```markdown
The native resource models the offchain cost of processing a transaction. Currently, this is dominated by proving and publishing data. A good intuition for it is "how many RISC-V cycles it takes to prove a given computation".
```

**File:** docs/double_resource_accounting.md (L19-19)
```markdown
If a transaction execution runs out of native resources, the entire transaction is reverted. If the same happens during transaction validation, the transaction is considered invalid.
```
