### Title
Unsafe `u64`-to-`i64` Cast in `compute_gas_refund` Silently Suppresses `delta_gas` Adjustment, Allowing L1 Transactions to Underpay Operator Fees - (File: `basic_bootloader/src/bootloader/transaction_flow/refund_calculation.rs`)

---

### Summary

In `compute_gas_refund`, the expression `(native_used / native_per_gas) as i64` performs an unchecked narrowing cast from `u64` to `i64`. For L1 transactions with a sufficiently large `gas_limit`, `native_used / native_per_gas` can exceed `i64::MAX`, causing the cast to produce a large negative number. This makes `delta_gas` appear negative, silently suppressing the upward adjustment to `gas_used`. The result is that the user is undercharged for native resource consumption, and the operator receives less fee than it is owed.

---

### Finding Description

`compute_gas_refund` implements the `deltaGas` adjustment described in the double-resource-accounting model:

```
deltaGas := (nativeUsed / nativePerGas) - gasUsed
if deltaGas > 0: gasUsed += deltaGas
```

The Rust implementation at line 72 is:

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

`native_used` and `native_per_gas` are both `u64`. The sub-expression `(native_used / native_per_gas)` is a `u64` value that can be as large as `u64::MAX` (≈ 1.84 × 10¹⁹). `i64::MAX` is ≈ 9.22 × 10¹⁸. When `native_used / native_per_gas > i64::MAX`, the `as i64` cast wraps to a large negative number (e.g., `u64::MAX as i64 == -1`). The `if delta_gas > 0` guard then silently skips the adjustment entirely.

**How the overflow is reached for L1 transactions:**

For L2 transactions, validation enforces `gas_limit * ERGS_PER_GAS < u64::MAX`, bounding `gas_limit ≤ u64::MAX / 256 ≈ 7.2 × 10¹⁶ < i64::MAX`. This makes the cast safe for L2 paths. [2](#0-1) 

L1 transactions have **no such bound**. `gas_limit` is read directly from the ABI-encoded transaction without an ergs-overflow check: [3](#0-2) 

`native_per_gas` for L1 transactions is computed as `gas_price / L1_TX_NATIVE_PRICE` (where `L1_TX_NATIVE_PRICE = 10`). With `gas_price = 10`, `native_per_gas = 1`.

`full_native_limit` is then:

```rust
gas_limit.saturating_mul(native_per_gas)  // = gas_limit * 1 = gas_limit
``` [4](#0-3) 

If `gas_limit > i64::MAX ≈ 9.22 × 10¹⁸` and the transaction consumes all native resources (`remaining_native ≈ 0`), then:

- `native_used ≈ gas_limit > i64::MAX`
- `native_used / native_per_gas = native_used > i64::MAX`
- `(native_used / native_per_gas) as i64` → large negative number
- `delta_gas < 0` → branch not taken
- `gas_used` is not adjusted upward

The true `delta_gas` (≈ `gas_limit − gas_used`) is large and positive, but is silently discarded.

The subsequent unchecked subtraction `gas_limit - gas_used` at line 83 does not underflow because `gas_used` (without the adjustment) is still bounded by `gas_limit`. [5](#0-4) 

The `require_internal!(total_gas_refund <= gas_limit, ...)` guard at line 85 always passes because `gas_used ≥ 0`, so no error is returned and the incorrect `gas_used` propagates silently.

---

### Impact Explanation

The `gas_used` value returned by `compute_gas_refund` is used to compute the operator fee payment and the user refund:

- **Operator receives**: `gas_used_underreported * gas_price` (less than owed)
- **User refund**: `(gas_limit - gas_used_underreported) * gas_price` (more than owed) [6](#0-5) 

The attacker (controlling the refund recipient) recovers tokens that should have been paid to the operator. In the extreme case (`gas_limit ≈ 10¹⁹`, `native_per_gas = 1`, `gas_price = 10`), the attacker pays near-zero operator fees despite consuming enormous native (proving) resources, effectively getting free proving work from the sequencer/prover.

This is a **resource accounting bug** and a **public funds-loss path** (operator fee theft).

---

### Likelihood Explanation

Triggering the bug requires an L1 transaction with `gas_limit > i64::MAX / native_per_gas`. For the minimum `native_per_gas = 1` (gas_price = 10), this means `gas_limit > 9.22 × 10¹⁸`. The attacker must deposit at least `gas_limit × gas_price ≈ 9.22 × 10¹⁹` base tokens on L1. This is a very high capital barrier, making exploitation unlikely in practice. However, the code path is reachable by any L1 transaction sender without any privileged access, and no on-chain guard prevents a large `gas_limit` from being submitted.

---

### Recommendation

Replace the unsafe narrowing cast with a checked conversion. If `native_used / native_per_gas` exceeds `i64::MAX`, the delta should be capped at `gas_limit - gas_used` (the maximum possible adjustment) rather than silently discarded:

```rust
let native_gas_equivalent = native_used / native_per_gas;
let delta_gas = if native_gas_equivalent > gas_used {
    // Safe: both operands are u64, result fits in u64
    let delta = native_gas_equivalent - gas_used;
    // Cap at remaining gas to avoid exceeding gas_limit
    delta.min(gas_limit - gas_used)
} else {
    0u64
};
gas_used += delta_gas;
```

This eliminates the signed intermediate entirely and avoids the cast hazard.

---

### Proof of Concept

1. Construct an L1 (priority) transaction with:
   - `gas_limit = 10_000_000_000_000_000_000u64` (10¹⁹, which is > `i64::MAX`)
   - `gas_price = 10` (so `native_per_gas = 10 / L1_TX_NATIVE_PRICE = 1`)
   - `total_deposited ≥ gas_limit * gas_price = 10²⁰` tokens
2. The transaction executes and consumes all native resources (`remaining_native ≈ 0`).
3. In `compute_gas_refund`:
   - `full_native_limit = 10¹⁹ * 1 = 10¹⁹`
   - `native_used ≈ 10¹⁹`
   - `native_used / native_per_gas = 10¹⁹`
   - `10¹⁹ as i64` → negative (since `10¹⁹ > i64::MAX ≈ 9.22 × 10¹⁸`)
   - `delta_gas < 0` → branch skipped
   - `gas_used` remains at its EVM-gas value (e.g., 21,000)
4. Operator receives `21,000 * 10 = 210,000` tokens instead of `≈ 10²⁰` tokens.
5. Refund recipient receives `(10¹⁹ - 21,000) * 10 ≈ 10²⁰` tokens. [7](#0-6)

### Citations

**File:** basic_bootloader/src/bootloader/transaction_flow/refund_calculation.rs (L59-63)
```rust
    let full_native_limit = if cfg!(feature = "unlimited_native") || native_per_gas == 0 {
        u64::MAX - 1
    } else {
        gas_limit.saturating_mul(native_per_gas)
    };
```

**File:** basic_bootloader/src/bootloader/transaction_flow/refund_calculation.rs (L66-89)
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

    let total_gas_refund = gas_limit - gas_used;
    system_log!(system, "Refund after accounting for unused gas, refund counters and native cost: {total_gas_refund}\n");
    require_internal!(
        total_gas_refund <= gas_limit,
        "Gas refund greater than gas limit",
        system
    )?;
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs (L69-73)
```rust
    require!(
        tx_gas_limit.saturating_mul(ERGS_PER_GAS) < u64::MAX,
        InvalidTransaction::CallerGasLimitTooHigh,
        system
    )?;
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L68-68)
```rust
    let gas_limit = transaction.gas_limit.read();
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L277-279)
```rust
    let pay_to_operator = U256::from(gas_used)
        .checked_mul(U256::from(gas_price))
        .ok_or(internal_error!("gu*gp"))?;
```
