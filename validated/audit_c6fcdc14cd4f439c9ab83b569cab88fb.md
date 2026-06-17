### Title
Implicit Unsigned-to-Signed Cast Produces Wrong `delta_gas` Sign, Corrupting Gas Refund Accounting — (`basic_bootloader/src/bootloader/transaction_flow/refund_calculation.rs`)

---

### Summary

`compute_gas_refund` computes a signed `delta_gas` by casting a `u64` division result directly to `i64`. When the quotient exceeds `i64::MAX`, Rust's `as` cast silently reinterprets the bit pattern as a large negative number. The sign check `if delta_gas > 0` then evaluates to `false`, so the extra native-resource cost is never added to `gas_used`, and the caller receives a larger-than-deserved gas refund.

---

### Finding Description

In `compute_gas_refund`, the native-resource-to-gas adjustment is computed as:

```rust
let delta_gas = if native_per_gas == 0 {
    0
} else {
    (native_used / native_per_gas) as i64 - (gas_used as i64)   // line 72
};

if delta_gas > 0 {
    gas_used += delta_gas as u64;   // line 78
}
``` [1](#0-0) 

`native_used` and `native_per_gas` are both `u64`. The sub-expression `native_used / native_per_gas` is therefore also `u64`. Rust's `as i64` cast is defined for all bit patterns but **does not check for overflow**: any value in the range `(i64::MAX, u64::MAX]` is reinterpreted as a negative `i64`. This is the direct Rust analog of the Solidity `int256(a - b)` pattern flagged in the reference report.

Two distinct mis-computation paths exist:

**Path A — quotient overflows `i64`:**
If `native_used / native_per_gas > i64::MAX`, the cast yields a large negative number. Subtracting a positive `gas_used as i64` makes `delta_gas` even more negative. The `if delta_gas > 0` guard is never entered, so the extra native cost is silently dropped and the user is undercharged.

**Path B — `gas_used` overflows `i64`:**
If `gas_used > i64::MAX`, `gas_used as i64` is negative. Combined with a positive `(native_used / native_per_gas) as i64`, `delta_gas` becomes a very large positive value. `gas_used += delta_gas as u64` then wraps `gas_used` around to a small value, making `total_gas_refund = gas_limit - gas_used` (line 83) an enormous number — a near-full refund of the gas limit. [2](#0-1) 

---

### Impact Explanation

- **Path A:** The transaction pays less gas than the native resources it consumed. The operator/sequencer absorbs the shortfall. This is a **resource accounting bug** leading to under-collection of fees.
- **Path B:** `gas_used` wraps to a tiny value, so `total_gas_refund ≈ gas_limit`. The caller receives a near-complete refund regardless of actual execution cost. This is a **public funds-loss path** — the sequencer/operator pays out a refund it should not.

Both paths affect the `RefundInfo` struct returned to the caller, which drives the actual ETH refund transferred back to the transaction sender. [3](#0-2) 

---

### Likelihood Explanation

The trigger condition is `gas_limit > i64::MAX` (~9.2 × 10¹⁸). `gas_limit` is a `u64` supplied directly by the transaction sender. No explicit upper-bound check on `gas_limit` is visible in `compute_gas_refund` or in the call sites examined. The block gas limit enforced by the sequencer is the only practical guard. On a ZKsync OS chain where the operator sets a permissive or absent block gas limit, an unprivileged sender can craft a transaction with `gas_limit = u64::MAX` and trigger Path B. Likelihood is **medium** given operator-controlled block gas limits, but the entry path requires no privilege.

---

### Recommendation

Replace the direct `as i64` casts with checked conversions that propagate an error on overflow, mirroring the pattern already used elsewhere in the file (`checked_sub`, `ok_or(internal_error!(...))`):

```rust
let native_gas_equiv = i64::try_from(native_used / native_per_gas)
    .map_err(|_| internal_error!("native_used/native_per_gas overflows i64"))?;
let gas_used_signed = i64::try_from(gas_used)
    .map_err(|_| internal_error!("gas_used overflows i64"))?;
let delta_gas = native_gas_equiv - gas_used_signed;
```

This is the Rust equivalent of replacing `int256(a-b)` with `int256(a)-int256(b)` as recommended in the reference report.

---

### Proof of Concept

**Path B (refund inflation):**

1. Craft a transaction with `gas_limit = 2^63 + 1` (just above `i64::MAX`).
2. Arrange execution so that `native_used` is small (e.g., minimal computation), making `native_used / native_per_gas` a small positive value that fits in `i64`.
3. After execution, `gas_used ≈ gas_limit = 2^63 + 1`. `gas_used as i64` wraps to `-(2^63 - 1)`.
4. `delta_gas = small_positive - (-(2^63-1)) ≈ 2^63`, a large positive value.
5. `gas_used += delta_gas as u64` → `(2^63 + 1) + 2^63 ≈ 2^64 + 1`, which wraps modulo `2^64` to `1`.
6. `total_gas_refund = gas_limit - gas_used = (2^63 + 1) - 1 = 2^63` — the caller receives a refund of nearly the entire gas limit. [4](#0-3)

### Citations

**File:** basic_bootloader/src/bootloader/transaction_flow/refund_calculation.rs (L11-18)
```rust
pub(crate) struct RefundInfo {
    // EVM gas used by the transaction
    pub(crate) gas_used: u64,
    // EVM-specific refund
    pub(crate) evm_refund: u64,
    // Total native resource used by the transaction (includes pubdata)
    pub(crate) native_used: u64,
}
```

**File:** basic_bootloader/src/bootloader/transaction_flow/refund_calculation.rs (L69-88)
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
        // TODO: return delta_gas to gas_used?
    }

    let total_gas_refund = gas_limit - gas_used;
    system_log!(system, "Refund after accounting for unused gas, refund counters and native cost: {total_gas_refund}\n");
    require_internal!(
        total_gas_refund <= gas_limit,
        "Gas refund greater than gas limit",
        system
```
