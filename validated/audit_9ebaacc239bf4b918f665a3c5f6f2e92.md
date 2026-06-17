### Title
Floor Division in `delta_gas` Calculation Undercharges Gas for Native Resource Consumption - (File: `basic_bootloader/src/bootloader/transaction_flow/refund_calculation.rs`)

### Summary
In `compute_gas_refund`, the conversion from native resource consumption to gas units uses Rust's default truncating (floor) integer division. This causes `delta_gas` to be smaller than it should be, meaning the user pays less gas for their native resource consumption than the protocol is owed. The rounding should favor the protocol (ceiling), not the user (floor).

### Finding Description
The ZKsync OS double-resource accounting model derives a `delta_gas` adjustment at the end of every transaction to ensure that native resource consumption is fully reflected in the gas charged. The formula, documented in `docs/double_resource_accounting.md`, is:

```
deltaGas := (nativeUsed / nativePerGas) - gasUsed
```

The implementation in `compute_gas_refund` is:

```rust
let delta_gas = if native_per_gas == 0 {
    0
} else {
    (native_used / native_per_gas) as i64 - (gas_used as i64)
};

if delta_gas > 0 {
    gas_used += delta_gas as u64;
}
``` [1](#0-0) 

The sub-expression `native_used / native_per_gas` uses Rust's `/` operator on `u64`, which is truncating (floor) division. When `native_used` is not an exact multiple of `native_per_gas`, the quotient is rounded **down**, making `delta_gas` one unit smaller than it should be. Consequently, `gas_used` is one unit smaller than it should be, and the user receives one extra gas unit in their refund.

This is directly inconsistent with how `native_per_gas` itself is computed during validation, which explicitly uses **ceiling** division to favor the protocol:

```rust
u256_try_to_u64(&gas_price.div_ceil(native_price))
``` [2](#0-1) 

The same ceiling-division pattern is used for L1 transactions:

```rust
u256_try_to_u64(&gas_price.div_ceil(native_price))
``` [3](#0-2) 

The `compute_gas_refund` function is called for both L2 (ZK) and Ethereum-style transaction flows: [4](#0-3) [5](#0-4) 

### Impact Explanation
Every transaction where `native_used % native_per_gas != 0` causes the protocol to undercharge by exactly 1 gas unit. The user's refund is inflated by `gas_price` tokens per such transaction. While the per-transaction loss is dust (at most 1 gas unit), it is:

1. **Guaranteed** — it occurs on every transaction where native consumption is not an exact multiple of `native_per_gas`.
2. **Accumulative** — across a high-throughput ZKsync OS chain, the aggregate loss grows unboundedly.
3. **Exploitable** — an attacker can craft transactions that maximize the rounding error by targeting `native_used ≡ native_per_gas − 1 (mod native_per_gas)`, ensuring the maximum 1-gas-unit underpayment on every submission.

The final `gas_used` feeds directly into the token transfer to the operator:

```rust
let token_to_pay_operator = U256::from(context.gas_used)
    .checked_mul(gas_price_for_operator)
    ...
``` [6](#0-5) 

So the operator (protocol) receives `gas_price` fewer tokens per affected transaction.

### Likelihood Explanation
The condition `native_used % native_per_gas != 0` holds for the vast majority of real transactions, since `native_used` is the sum of many heterogeneous native charges (EVM opcode costs, pubdata, bootloader overhead) and is unlikely to be an exact multiple of `native_per_gas`. Every unprivileged L2 or L1→L2 transaction sender triggers this path.

### Recommendation
Replace the floor division with ceiling division in `compute_gas_refund`:

```rust
// Before (floor division — favors user):
(native_used / native_per_gas) as i64 - (gas_used as i64)

// After (ceiling division — favors protocol, consistent with native_per_gas derivation):
native_used.div_ceil(native_per_gas) as i64 - (gas_used as i64)
``` [1](#0-0) 

This is consistent with the ceiling division already applied when deriving `native_per_gas` from `gas_price` and `native_price`.

### Proof of Concept
Concrete numeric example:

- `native_per_gas = 7`
- `native_used = 100` (i.e., `100 mod 7 = 2 ≠ 0`)
- `gas_used` (from ergs) = 10

Current code:
```
delta_gas = (100 / 7) - 10 = 14 - 10 = 4
gas_used_final = 10 + 4 = 14
```

Correct (ceiling):
```
delta_gas = ceil(100 / 7) - 10 = 15 - 10 = 5
gas_used_final = 10 + 5 = 15
```

The user pays for 14 gas units instead of 15. The operator receives `gas_price` fewer tokens. Repeated across every transaction on the chain, the cumulative protocol loss is unbounded.

### Citations

**File:** basic_bootloader/src/bootloader/transaction_flow/refund_calculation.rs (L69-79)
```rust
        let delta_gas = if native_per_gas == 0 {
            0
        } else {
            (native_used / native_per_gas) as i64 - (gas_used as i64)
        };

        if delta_gas > 0 {
            // In this case, the native resource consumption is more than the
            // gas consumption accounted for. Consume extra gas.
            gas_used += delta_gas as u64;
        }
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs (L135-138)
```rust
            u256_try_to_u64(&gas_price.div_ceil(native_price)).ok_or(TxError::Validation(
                InvalidTransaction::NativeResourcesAreTooExpensive,
            ))?
        }
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L262-273)
```rust
    let RefundInfo {
        gas_used,
        evm_refund,
        native_used,
    } = compute_gas_refund(
        system,
        to_charge_for_pubdata,
        gas_limit,
        minimal_gas_used,
        native_per_gas,
        &mut resources,
    )?;
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L469-474)
```rust
            u256_try_to_u64(&gas_price.div_ceil(native_price)).unwrap_or_else(|| {
                system_log!(
                    system,
                    "Native per gas calculation for L1 tx overflows, using saturated arithmetic instead");
                u64::MAX
            })
```

**File:** basic_bootloader/src/bootloader/transaction_flow/ethereum/mod.rs (L477-485)
```rust
        let refund_info = compute_gas_refund(
            system,
            S::Resources::empty(),
            transaction.gas_limit(),
            min_gas_used,
            0u64,
            &mut context.resources.main_resources,
        )?;
        context.gas_used = refund_info.gas_used;
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/mod.rs (L514-516)
```rust
        let token_to_pay_operator = U256::from(context.gas_used)
            .checked_mul(gas_price_for_operator)
            .ok_or(internal_error!("gu*gpfo"))?;
```
