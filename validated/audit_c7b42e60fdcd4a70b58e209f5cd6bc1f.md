### Title
Precision Loss in `native_per_pubdata` and `delta_gas` Calculations Due to Floor Division - (`basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs`, `basic_bootloader/src/bootloader/transaction_flow/refund_calculation.rs`)

---

### Summary

ZKsync OS uses integer floor division in two critical resource-accounting calculations: `native_per_pubdata` (the native cost per pubdata byte) and `delta_gas` (the gas adjustment for native resource consumption). Both truncate fractional results, causing systematic undercharging of users. The combined effect mirrors the external report's pattern: two independent precision-loss sources compound to allow users to consume more resources than they pay for, at the operator's expense.

---

### Finding Description

**Source 1 — `native_per_pubdata` floor division**

In `validation_impl.rs` line 142, the per-pubdata-byte native cost is computed as:

```rust
let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price))
    .ok_or(TxError::Validation(InvalidTransaction::PubdataPriceTooHigh))?;
```

`wrapping_div` is Rust's truncating (floor) integer division. The true cost per byte is `pubdata_price / native_price` (real-valued), but the charged amount is `⌊pubdata_price / native_price⌋`. The discarded fractional part is `(pubdata_price % native_price) / native_price` per byte. In the worst case (e.g., `pubdata_price = native_price + 1`), the relative undercharge is `1/native_price` — up to ~10% if `native_price = 10`, ~1% if `native_price = 100`.

The same floor division appears in the off-chain helper `api/src/helpers.rs` line 427, which is used for pre-flight validation:

```rust
let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price)).ok_or(())?;
```

Both paths produce the same truncated value, so the undercharge is consistent between simulation and execution.

**Source 2 — `delta_gas` floor division**

In `refund_calculation.rs` line 72, the gas adjustment for native resource consumption is:

```rust
(native_used / native_per_gas) as i64 - (gas_used as i64)
```

`native_used / native_per_gas` is again floor division. The true implied gas from native consumption is `native_used / native_per_gas` (real-valued), but the charged delta is `⌊native_used / native_per_gas⌋`. The maximum undercharge per transaction is `(native_per_gas − 1) / native_per_gas` of one gas unit — small individually, but systematic across all transactions.

**Combined effect**

A user who generates significant pubdata benefits from both truncations simultaneously:
- They pay `⌊pubdata_price / native_price⌋` native units per pubdata byte instead of the true ratio.
- Their gas adjustment for native consumption is also rounded down.

The operator must pay the full L1 pubdata cost but collects less from users. Neither truncation is configurable — both are hardcoded arithmetic operations in the bootloader.

---

### Impact Explanation

The operator sets `pubdata_price` and `native_price` to reflect real L1 costs. Whenever `pubdata_price % native_price ≠ 0` (which is the common case since both values are derived from independent market signals), every transaction that writes pubdata is systematically undercharged. A user who generates `N` bytes of pubdata pays `N × ⌊pubdata_price / native_price⌋` native units instead of `N × pubdata_price / native_price`. The shortfall is `N × (pubdata_price % native_price) / native_price` native units per transaction, borne by the operator. At scale (many transactions, large pubdata), this is a material loss of operator revenue.

---

### Likelihood Explanation

`pubdata_price` and `native_price` are independently derived from L1 gas prices and proving costs. There is no mechanism that forces `pubdata_price` to be an exact multiple of `native_price`. In practice, these values will almost never be exact multiples, so the precision loss occurs on virtually every transaction that writes pubdata.

---

### Recommendation

1. Use ceiling division for `native_per_pubdata` so users are charged at least the true cost per pubdata byte:
   ```rust
   let native_per_pubdata = u256_try_to_u64(&pubdata_price.div_ceil(native_price))
       .ok_or(TxError::Validation(InvalidTransaction::PubdataPriceTooHigh))?;
   ```
   This is consistent with how `native_per_gas` is already computed (using `div_ceil`).

2. Apply the same fix in `api/src/helpers.rs` line 427 to keep simulation and execution consistent.

3. For `delta_gas`, consider using ceiling division or adding 1 to ensure the operator is not systematically undercompensated for native resource consumption.

---

### Proof of Concept

**Concrete example for `native_per_pubdata`:**

- `pubdata_price = 150`, `native_price = 100`
- Charged: `⌊150 / 100⌋ = 1` native unit per pubdata byte
- True cost: `1.5` native units per pubdata byte
- Undercharge: **33% per pubdata byte**

A transaction generating 1,000 bytes of pubdata pays 1,000 native units instead of 1,500 — a shortfall of 500 native units that the operator absorbs.

**Concrete example for `delta_gas`:**

- `native_per_gas = 100`, `native_used = 10_099`
- `delta_gas = ⌊10_099 / 100⌋ − gas_used = 100 − gas_used`
- True implied gas from native: `100.99`
- Undercharge: `0.99` gas units (small per transaction, but systematic)

**Code references:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs (L134-138)
```rust
        } else {
            u256_try_to_u64(&gas_price.div_ceil(native_price)).ok_or(TxError::Validation(
                InvalidTransaction::NativeResourcesAreTooExpensive,
            ))?
        }
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs (L141-143)
```rust
    // We checked native_price != 0 above
    let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price))
        .ok_or(TxError::Validation(InvalidTransaction::PubdataPriceTooHigh))?;
```

**File:** api/src/helpers.rs (L426-427)
```rust
    // native_per_pubdata = pubdata_price / native_price
    let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price)).ok_or(())?;
```

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
