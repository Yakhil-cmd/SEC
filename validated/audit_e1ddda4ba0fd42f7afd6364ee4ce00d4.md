Now I have a complete picture. Let me analyze the exact analog.

The original bug has two interacting components:
1. `_minutesPassedSinceLastFeeOp()` truncates time (rounds down)
2. `_updateLastFeeOpTime` only updates when `timePassed >= 1 minutes` (threshold gate)

Combined: the decay is slower than intended because the time measurement is systematically underestimated and the gate prevents correction.

In ZKsync OS, `compute_gas_refund` in `refund_calculation.rs` has the exact same two-component structure:
1. `native_used / native_per_gas` truncates (integer floor division) — line 72
2. `if delta_gas > 0` is a threshold gate that only applies the correction when positive — line 75

Combined: users systematically underpay for native resource consumption because the gas-equivalent of native used is underestimated and the gate prevents the correction from firing when truncation pushes `delta_gas` to zero.

Additionally, `native_per_pubdata` in `validation_impl.rs` line 142 uses `wrapping_div` (floor), compounding the underpayment on the pubdata side.

---

### Title
Integer Division Truncation in `delta_gas` Calculation Causes Systematic Underpayment for Native Resource Consumption — (File: `basic_bootloader/src/bootloader/transaction_flow/refund_calculation.rs`)

### Summary
`compute_gas_refund` computes `delta_gas` using integer floor division `native_used / native_per_gas`. Combined with the `if delta_gas > 0` threshold gate, this mirrors the exact two-component pattern from the referenced finding: truncation plus a conditional update. The result is that users systematically underpay for native resource consumption on every transaction where native cost exceeds EVM gas cost.

### Finding Description

**Component 1 — truncation** (`refund_calculation.rs`, line 72):

```rust
(native_used / native_per_gas) as i64 - (gas_used as i64)
```

`native_used / native_per_gas` is integer floor division. The true gas-equivalent of native consumed is `native_used / native_per_gas` (real-valued). The floor truncates this by up to `(native_per_gas - 1) / native_per_gas`, i.e., up to nearly 1 full gas unit.

**Component 2 — threshold gate** (`refund_calculation.rs`, lines 75–79):

```rust
if delta_gas > 0 {
    gas_used += delta_gas as u64;
}
```

The correction is only applied when `delta_gas > 0`. When truncation pushes the true fractional `delta_gas` (e.g., `0.97`) down to `0`, the gate suppresses the correction entirely. The user pays zero extra gas even though native consumption warrants ~1 extra gas unit.

**Compounding factor — `native_per_pubdata` floor division** (`validation_impl.rs`, line 142):

```rust
let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price))
    .ok_or(TxError::Validation(InvalidTransaction::PubdataPriceTooHigh))?;
```

`wrapping_div` is floor division. The true pubdata cost per byte is `pubdata_price / native_price` native units; the charged rate is `floor(pubdata_price / native_price)`. This reduces `native_used` (less native is consumed for pubdata), which in turn reduces `delta_gas`, compounding the underpayment.

Note: `native_per_gas` itself is computed with `div_ceil` (rounds up), which makes `native_used / native_per_gas` even smaller, widening the gap further.

### Impact Explanation

Every transaction where native resource consumption exceeds EVM gas consumption (i.e., `delta_gas` should be positive) is affected. The user pays up to 1 gas unit less than the true native cost warrants per transaction. The monetary shortfall per transaction is bounded by `gas_price` wei. At scale (high-throughput L2), this is a systematic, accumulating loss for the operator/protocol. The `native_per_pubdata` floor division adds a secondary underpayment of up to `pubdata_used` native resources per transaction, whose gas-equivalent is `pubdata_used / native_per_gas` gas units.

### Likelihood Explanation

High. Any transaction whose proving/native cost exceeds its EVM gas cost triggers this path. This is the normal case for transactions that write storage (pubdata-heavy) or invoke expensive precompiles. The truncation fires on every such transaction without any special attacker setup — a normal user submitting a standard ERC-20 transfer or storage-writing call is sufficient.

### Recommendation

Replace floor division with ceiling division in `delta_gas`:

```rust
let delta_gas = if native_per_gas == 0 {
    0
} else {
    native_used.div_ceil(native_per_gas) as i64 - (gas_used as i64)
};
```

Similarly, replace `wrapping_div` with `div_ceil` for `native_per_pubdata` in `validation_impl.rs`:

```rust
let native_per_pubdata = u256_try_to_u64(&pubdata_price.div_ceil(native_price))
    .ok_or(TxError::Validation(InvalidTransaction::PubdataPriceTooHigh))?;
```

This ensures the gas-equivalent of native consumption is never underestimated and the threshold gate cannot suppress a legitimate correction.

### Proof of Concept

**Setup**: `gas_price = 100`, `native_price = 3`

**Step 1** — `native_per_gas` at validation (`validation_impl.rs` line 135):
```
native_per_gas = ceil(100 / 3) = 34
```

**Step 2** — Transaction executes, consuming `native_used = 67` native resources and `gas_used = 1` EVM gas.

**Step 3** — `delta_gas` at refund (`refund_calculation.rs` line 72):
```
delta_gas = floor(67 / 34) - 1 = 1 - 1 = 0
```
Gate at line 75: `delta_gas > 0` is false → **no adjustment**.

**Step 4** — True gas-equivalent of native used:
```
67 / 34 = 1.97...  →  true delta_gas ≈ 0.97
```
The user should pay ~1 extra gas unit (≈ 100 wei at `gas_price = 100`), but pays nothing extra.

**Attacker entry path**: Any unprivileged L2 transaction sender. No special access required. The sender simply submits a transaction whose native resource consumption slightly exceeds `gas_used * native_per_gas` — a condition that arises naturally for storage-writing or precompile-calling transactions. [1](#0-0) [2](#0-1) [3](#0-2)

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

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs (L141-143)
```rust
    // We checked native_price != 0 above
    let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price))
        .ok_or(TxError::Validation(InvalidTransaction::PubdataPriceTooHigh))?;
```

**File:** docs/double_resource_accounting.md (L47-50)
```markdown
Then we compute the difference between the implicit gas used derived from native resource consumption and the gas used by EEs from the ergs used. We call this value `deltaGas`.
  `deltaGas := (nativeUsed / nativePerGas) - gasUsed`

If `deltaGas > 0`, we add it to `gasUsed` and charge it from ergs. This ensures that gas estimation will include additional gas to cover for native resources using just base fee. We expect the base fee to be enough to cover most transactions without the need of additional gas.
```
