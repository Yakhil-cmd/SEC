### Title
Asymmetric `delta_gas` Adjustment in `compute_gas_refund` Causes Users to Systematically Overpay Fees — (`File: basic_bootloader/src/bootloader/transaction_flow/refund_calculation.rs`)

---

### Summary

`compute_gas_refund` implements ZKsync OS's double resource accounting reconciliation. When the native resource consumption is *less* than the EVM gas consumption (`delta_gas < 0`), `gas_used` is never reduced to reflect the lower native cost. Users are charged for the full EVM gas amount even when the native resource equivalent is smaller. An in-code `TODO` comment explicitly acknowledges this unresolved gap. The result is a systematic overcharge of user fees whenever EVM gas exceeds the native resource equivalent — the direct analog of the Cork Protocol's unchecked `addLiquidity()` return values leaving a residual amount unaccounted for.

---

### Finding Description

`compute_gas_refund` computes `delta_gas` as the signed difference between the native-resource-implied gas cost and the EVM gas used:

```rust
let delta_gas = if native_per_gas == 0 {
    0
} else {
    (native_used / native_per_gas) as i64 - (gas_used as i64)
};

if delta_gas > 0 {
    // native consumption > EVM gas → charge extra
    gas_used += delta_gas as u64;
}
// TODO: return delta_gas to gas_used?
``` [1](#0-0) 

The `delta_gas > 0` branch correctly charges extra gas when native resources are the bottleneck. However, the `delta_gas < 0` branch — where EVM gas consumption exceeds the native resource equivalent — is silently ignored. The `TODO` comment on line 80 explicitly asks whether `gas_used` should be reduced in this case, confirming the gap is known but unresolved.

There is a second compounding issue: the expression `native_used / native_per_gas` uses integer (floor) division. Even when native and EVM costs are exactly proportional, truncation systematically underestimates the native-resource-implied gas, biasing `delta_gas` negative and widening the overcharge.

The `native_used` and `gas_used` values are derived from the remaining resources after execution:

```rust
let mut gas_used = gas_limit
    .checked_sub(resources.ergs().0.div_floor(ERGS_PER_GAS))
    ...;
resources.exhaust_ergs();
...
let native_used = full_native_limit.saturating_sub(resources.native().remaining().as_u64());
``` [2](#0-1) 

The resulting `gas_used` is then used directly to compute the user refund and operator payment in both the ZK L2 flow:

```rust
let token_to_refund =
    context.gas_price * U256::from(context.tx_gas_limit - context.gas_used);
``` [3](#0-2) 

and the Ethereum-compatible flow:

```rust
let refund = context.tx_level_metadata.tx_gas_price
    * U256::from(context.tx_gas_limit - context.gas_used);
``` [4](#0-3) 

Because `gas_used` is inflated (not reduced by `|delta_gas|` when `delta_gas < 0`), the refund is smaller than it should be, and the operator receives the difference.

The double resource accounting design is documented as:

> `deltaGas := (nativeUsed / nativePerGas) - gasUsed`. If `deltaGas > 0`, we add it to `gasUsed`… [5](#0-4) 

The documentation describes only the `delta_gas > 0` case; the `delta_gas < 0` case is unspecified, consistent with the in-code TODO.

---

### Impact Explanation

Every transaction where EVM gas consumption exceeds the native resource equivalent results in the user being charged `|delta_gas| * gas_price` more than the native resource cost would require. These excess fees flow to the operator rather than being refunded to the sender. The overcharge is:

```
overcharge = |delta_gas| * gas_price
           = (gas_used - native_used / native_per_gas) * gas_price
```

Additionally, integer truncation in `native_used / native_per_gas` introduces a systematic rounding error that always favors the operator, even in the "balanced" case. Over many transactions this accumulates into a material, predictable loss for users — directly analogous to the Cork Protocol's 1–2 wei residuals accumulating over time.

---

### Likelihood Explanation

The `delta_gas < 0` condition is the *common* case. It occurs whenever a transaction is EVM-computation-heavy but proving-light (e.g., arithmetic-intensive contracts, memory-heavy operations, transactions with few storage writes). The documentation itself states: *"We expect the base fee to be enough to cover most transactions without the need of additional gas"* — implying `delta_gas > 0` is the exceptional case. Therefore, the overcharge affects the majority of ordinary L2 transactions.

---

### Recommendation

Implement the acknowledged TODO: when `delta_gas < 0`, reduce `gas_used` by `|delta_gas|` so that users are refunded the difference between EVM gas consumption and the native resource equivalent:

```rust
if delta_gas > 0 {
    gas_used += delta_gas as u64;
} else if delta_gas < 0 {
    // Native consumption < EVM gas → refund the difference
    gas_used = gas_used.saturating_sub((-delta_gas) as u64);
}
```

This ensures users pay `max(evm_gas, native_equivalent)` as intended by the double-accounting model, and that the truncation rounding error does not systematically disadvantage users.

---

### Proof of Concept

Consider a transaction with:
- `gas_limit = 100_000`, `gas_price = 1_000`
- EVM execution uses `gas_used = 60_000` gas
- Native resource consumption: `native_used = 30_000`, `native_per_gas = 1`
- `delta_gas = 30_000 - 60_000 = -30_000`

**Current behavior**: `gas_used` stays at `60_000`. User refund = `(100_000 - 60_000) * 1_000 = 40_000_000`. Operator receives `60_000 * 1_000 = 60_000_000`.

**Correct behavior**: `gas_used` reduced to `30_000`. User refund = `(100_000 - 30_000) * 1_000 = 70_000_000`. Operator receives `30_000 * 1_000 = 30_000_000`.

The user is overcharged by `30_000 * 1_000 = 30_000_000` units per transaction. For high-throughput blocks with many such transactions, the cumulative overcharge is significant and predictable.

### Citations

**File:** basic_bootloader/src/bootloader/transaction_flow/refund_calculation.rs (L31-64)
```rust
    let mut gas_used = gas_limit
        .checked_sub(resources.ergs().0.div_floor(ERGS_PER_GAS))
        .ok_or(internal_error!("gas remaining > gas limit"))?;
    resources.exhaust_ergs();

    system_log!(system, "Gas used before refund calculations: {gas_used}\n");

    // Following EIP-3529, refunds are capped to 1/5 of the gas used
    let evm_refund = {
        let full_refund_ergs = system.io.get_refund_counter().ergs();
        let full_refund_gas = full_refund_ergs.0.div_floor(ERGS_PER_GAS);
        let max_refund = gas_used / 5;
        core::cmp::min(full_refund_gas, max_refund)
    };

    system_log!(system, "Gas refund from refund counters = {evm_refund}\n");

    gas_used -= evm_refund;

    system_log!(
        system,
        "Minimal gas used from validation = {minimal_gas_used}\n"
    );

    #[allow(unused_mut)]
    let mut gas_used = core::cmp::max(gas_used, minimal_gas_used);

    // Note: for zero gas price, we use "unlimited native"
    let full_native_limit = if cfg!(feature = "unlimited_native") || native_per_gas == 0 {
        u64::MAX - 1
    } else {
        gas_limit.saturating_mul(native_per_gas)
    };
    let native_used = full_native_limit.saturating_sub(resources.native().remaining().as_u64());
```

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

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/mod.rs (L457-458)
```rust
            let token_to_refund =
                context.gas_price * U256::from(context.tx_gas_limit - context.gas_used); // can not overflow
```

**File:** basic_bootloader/src/bootloader/transaction_flow/ethereum/mod.rs (L517-518)
```rust
            let refund = context.tx_level_metadata.tx_gas_price
                * U256::from(context.tx_gas_limit - context.gas_used); // can not overflow
```

**File:** docs/double_resource_accounting.md (L47-51)
```markdown
Then we compute the difference between the implicit gas used derived from native resource consumption and the gas used by EEs from the ergs used. We call this value `deltaGas`.
  `deltaGas := (nativeUsed / nativePerGas) - gasUsed`

If `deltaGas > 0`, we add it to `gasUsed` and charge it from ergs. This ensures that gas estimation will include additional gas to cover for native resources using just base fee. We expect the base fee to be enough to cover most transactions without the need of additional gas.

```
