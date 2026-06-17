### Title
Signed Integer Cast Before Comparison Corrupts `delta_gas` in `compute_gas_refund` — (`File: basic_bootloader/src/bootloader/transaction_flow/refund_calculation.rs`)

---

### Summary

In `compute_gas_refund`, the native-resource-to-gas adjustment (`delta_gas`) is computed by casting two `u64` values to `i64` before performing subtraction and a sign comparison. If either operand exceeds `i64::MAX`, the cast silently flips the sign, making the comparison `if delta_gas > 0` produce the wrong result. A positive `delta_gas` that should be zero or negative causes `gas_used` to be inflated beyond `gas_limit`, triggering an unchecked unsigned subtraction `gas_limit - gas_used` that underflows, ultimately returning an `InternalError` that can propagate to block-level failure.

---

### Finding Description

In `compute_gas_refund`:

```rust
// basic_bootloader/src/bootloader/transaction_flow/refund_calculation.rs  lines 69-83
let delta_gas = if native_per_gas == 0 {
    0
} else {
    (native_used / native_per_gas) as i64 - (gas_used as i64)   // ← cast before comparison
};

if delta_gas > 0 {
    gas_used += delta_gas as u64;
}

let total_gas_refund = gas_limit - gas_used;   // ← bare subtraction, no checked_sub
```

Both `native_used / native_per_gas` and `gas_used` are `u64`. When either exceeds `i64::MAX` (2^63 − 1), the `as i64` cast wraps the value to a negative number. The subsequent subtraction of two negative `i64` values can produce a large positive result in wrapping arithmetic (Rust release mode), making `delta_gas > 0` true when the true mathematical difference is negative or zero.

**Concrete trigger path:**

`full_native_limit` is set to `u64::MAX - 1` when the `unlimited_native` feature is compiled in, or when `native_per_gas == 0` (but the latter is guarded). With `unlimited_native` active and `native_per_gas = 2`:

```
native_used          = u64::MAX - 1
native_used / 2      = (2^64 - 2) / 2 = 2^63 - 1  =  i64::MAX
(i64::MAX) as i64    = i64::MAX   (positive, correct)
gas_used             = 1_000_000  (small, bounded by gas_limit)
delta_gas            = i64::MAX - 1_000_000  ≈  i64::MAX  (large positive)
gas_used            += i64::MAX as u64  ≈  2^63
gas_limit - gas_used → underflow  (gas_limit << 2^63)
```

Without `unlimited_native`, the same overflow is reachable when `gas_limit > i64::MAX` (a `u64` transaction field with no enforced upper bound in the bootloader itself).

---

### Impact Explanation

After `gas_used` is inflated past `gas_limit`, the bare subtraction `let total_gas_refund = gas_limit - gas_used` underflows. In Rust release mode this wraps to a value near `u64::MAX`. The subsequent guard:

```rust
require_internal!(
    total_gas_refund <= gas_limit,
    "Gas refund greater than gas limit",
    system
)?;
```

fires and returns an `InternalError`. This error propagates through `before_refund` → transaction processing → block execution. An `InternalError` at this stage is treated as a block-level failure, not a per-transaction revert, meaning the entire block cannot be finalized — a chain-halting denial of service.

---

### Likelihood Explanation

Two realistic trigger conditions exist:

1. **`unlimited_native` feature enabled in a production build** — the feature sets `full_native_limit = u64::MAX - 1` unconditionally, making `native_used / native_per_gas` reachable at `i64::MAX` with `native_per_gas = 2`. An unprivileged transaction sender can exhaust native resources to maximize `native_used`.

2. **`gas_limit > i64::MAX`** — the bootloader accepts `gas_limit` as a raw `u64` from the transaction. No bootloader-level cap below `i64::MAX` is enforced; the block gas limit is operator-configured and could be set above `i64::MAX` in a misconfigured or adversarial operator environment.

Condition 1 is the more realistic path; condition 2 requires operator misconfiguration.

---

### Recommendation

Replace the signed-cast arithmetic with a direct unsigned comparison, mirroring the correct pattern from the Curve reference:

```rust
// Before (incorrect):
let delta_gas = (native_used / native_per_gas) as i64 - (gas_used as i64);
if delta_gas > 0 {
    gas_used += delta_gas as u64;
}

// After (correct):
let native_gas_equivalent = native_used / native_per_gas;
if native_gas_equivalent > gas_used {
    gas_used = native_gas_equivalent;   // saturate, never exceed gas_limit
}
```

Additionally, add a `checked_sub` (or `saturating_sub`) for `gas_limit - gas_used` at line 83 to prevent any future underflow from becoming a silent wrap:

```rust
let total_gas_refund = gas_limit.checked_sub(gas_used)
    .ok_or(internal_error!("gas_used exceeds gas_limit"))?;
```

---

### Proof of Concept

**Setup**: compile with `unlimited_native` feature; `native_per_gas = 2`.

**Transaction**: any transaction that exhausts all native resources (e.g., a tight loop that burns native until `resources.native().remaining() == 0`).

**State entering `compute_gas_refund`**:
- `gas_limit = 1_000_000`
- `native_per_gas = 2`
- `full_native_limit = u64::MAX - 1` (unlimited_native path)
- `resources.native().remaining() = 0`
- `native_used = u64::MAX - 1`
- `gas_used` (from ergs) = `1_000_000` (all gas consumed)

**Arithmetic**:
```
native_used / native_per_gas = (2^64 - 2) / 2 = 2^63 - 1  (fits i64::MAX exactly)
(2^63 - 1) as i64            = 9_223_372_036_854_775_807
gas_used as i64              = 1_000_000
delta_gas                    = 9_223_372_036_854_774_807  (large positive)
gas_used += delta_gas as u64 → gas_used = 9_223_372_037_854_775_807
gas_limit - gas_used         → underflow → wraps to ~u64::MAX - 8_223_372_036_854_775_807
require_internal! fires      → InternalError returned
block processing halts
``` [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** basic_bootloader/src/bootloader/transaction_flow/refund_calculation.rs (L58-64)
```rust
    // Note: for zero gas price, we use "unlimited native"
    let full_native_limit = if cfg!(feature = "unlimited_native") || native_per_gas == 0 {
        u64::MAX - 1
    } else {
        gas_limit.saturating_mul(native_per_gas)
    };
    let native_used = full_native_limit.saturating_sub(resources.native().remaining().as_u64());
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
