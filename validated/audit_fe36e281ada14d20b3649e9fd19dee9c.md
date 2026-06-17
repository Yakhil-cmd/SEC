### Title
Floor Division in `delta_gas` Calculation Allows Systematic Underpayment of Native Resource Costs - (`basic_bootloader/src/bootloader/transaction_flow/refund_calculation.rs`)

### Summary

In `compute_gas_refund`, the `delta_gas` calculation uses integer floor division (`native_used / native_per_gas`), which truncates the result downward. This allows any unprivileged transaction sender to underpay for native resource (proving cost) consumption by up to `(native_per_gas - 1)` gas units per transaction. The underpayment is systematic, repeatable, and compounds across many transactions.

### Finding Description

The `compute_gas_refund` function computes `delta_gas` — the extra gas to charge when native resource consumption exceeds the gas-based payment — as follows:

```rust
let delta_gas = if native_per_gas == 0 {
    0
} else {
    (native_used / native_per_gas) as i64 - (gas_used as i64)
};
``` [1](#0-0) 

The expression `native_used / native_per_gas` uses Rust's integer floor division, truncating any fractional remainder toward zero. The documentation explicitly defines the intended formula as:

> `deltaGas := (nativeUsed / nativePerGas) - gasUsed` [2](#0-1) 

Because the division floors, `delta_gas` is always ≤ the mathematically correct value. When `native_used % native_per_gas != 0`, the user is charged fewer gas units than they should be, and the protocol absorbs the difference in proving cost.

The `native_per_gas` ratio is computed with `div_ceil` (rounding up) to protect the protocol:

```rust
u256_try_to_u64(&gas_price.div_ceil(native_price))
``` [3](#0-2) 

But the inverse conversion back from native units to gas units in `delta_gas` uses floor division, negating that protection. The two rounding directions are asymmetric: `native_per_gas` rounds up (correct), but `native_used / native_per_gas` rounds down (incorrect).

A second, compounding rounding-down occurs in `native_per_pubdata`:

```rust
let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price))
    .ok_or(TxError::Validation(InvalidTransaction::PubdataPriceTooHigh))?;
``` [4](#0-3) 

`wrapping_div` is floor division. A lower `native_per_pubdata` means less native is charged for pubdata, which lowers `native_used`, which in turn lowers `delta_gas`, further reducing the user's ETH payment. Both rounding errors compound in the user's favor.

### Impact Explanation

The user's final ETH payment is `gas_used * gas_price`, where `gas_used` is increased by `delta_gas` when native consumption exceeds the gas-based budget. Because `delta_gas` is floored:

- Maximum underpayment per transaction: `(native_per_gas - 1)` gas units = `(native_per_gas - 1) * gas_price` wei.
- For example, with `gas_price = 1000` and `native_price = 10`, `native_per_gas = 100`, so up to 99 gas units (99,000 wei) can be avoided per transaction.
- An attacker who crafts transactions to land `native_used` just below a multiple of `native_per_gas` maximizes the truncation effect on every submission.
- Over many transactions the underpayment accumulates, shifting proving costs from users to the protocol.

This is a **resource accounting bug** — the protocol systematically under-charges for native (proving) resource consumption, analogous to the bonding curve under-charging for token purchases.

### Likelihood Explanation

The condition `native_used % native_per_gas != 0` is satisfied by virtually every real transaction, since `native_used` is determined by execution and is not aligned to `native_per_gas`. No special privileges are required; any L2 transaction sender triggers this path. The `before_refund` hook calls `compute_gas_refund` for every processed L2 transaction: [5](#0-4) 

Likelihood is **high** — it affects every transaction where native consumption is not an exact multiple of `native_per_gas`.

### Recommendation

Replace floor division with ceiling division in the `delta_gas` calculation:

```rust
// Before (floors, under-charges):
(native_used / native_per_gas) as i64 - (gas_used as i64)

// After (ceilings, correct):
native_used.div_ceil(native_per_gas) as i64 - (gas_used as i64)
``` [6](#0-5) 

Similarly, replace `wrapping_div` with `div_ceil` for `native_per_pubdata` in `validation_impl.rs`: [4](#0-3) 

### Proof of Concept

**Setup:**
- `native_price = 10`, `gas_price = 1000` → `native_per_gas = ceil(1000/10) = 100`
- Transaction executes with `gas_used = 1` (EVM gas from ergs)
- Native resource consumed: `native_used = 199`

**Current behavior (floor division):**
```
delta_gas = floor(199 / 100) - 1 = 1 - 1 = 0
```
No extra gas charged. User pays `1 * 1000 = 1000` wei.

**Correct behavior (ceiling division):**
```
delta_gas = ceil(199 / 100) - 1 = 2 - 1 = 1
```
One extra gas unit charged. User pays `2 * 1000 = 2000` wei.

**Underpayment:** 1 gas unit = 1000 wei per transaction. The attacker can repeat this for every transaction, choosing execution paths that keep `native_used % native_per_gas` just below `native_per_gas`, maximizing the truncation effect. The protocol absorbs the uncompensated proving cost on every block. [7](#0-6)

### Citations

**File:** basic_bootloader/src/bootloader/transaction_flow/refund_calculation.rs (L66-81)
```rust
    #[cfg(not(feature = "unlimited_native"))]
    {
        // Adjust gas_used with difference with used native
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
        // TODO: return delta_gas to gas_used?
    }
```

**File:** docs/double_resource_accounting.md (L48-50)
```markdown
  `deltaGas := (nativeUsed / nativePerGas) - gasUsed`

If `deltaGas > 0`, we add it to `gasUsed` and charge it from ergs. This ensures that gas estimation will include additional gas to cover for native resources using just base fee. We expect the base fee to be enough to cover most transactions without the need of additional gas.
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs (L135-138)
```rust
            u256_try_to_u64(&gas_price.div_ceil(native_price)).ok_or(TxError::Validation(
                InvalidTransaction::NativeResourcesAreTooExpensive,
            ))?
        }
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs (L142-143)
```rust
    let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price))
        .ok_or(TxError::Validation(InvalidTransaction::PubdataPriceTooHigh))?;
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/mod.rs (L427-434)
```rust
        let refund_info = compute_gas_refund(
            system,
            to_charge_for_pubdata,
            transaction.gas_limit(),
            min_gas_used,
            context.native_per_gas,
            &mut context.resources.main_resources,
        )?;
```
