### Title
`native_per_pubdata` Truncates to Zero via Floor Division, Enabling Fee-Less Pubdata Generation - (`File: basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs`)

---

### Summary

In `validate_and_compute_fee_for_transaction`, the `native_per_pubdata` ratio is computed using floor division (`wrapping_div`). When `pubdata_price < native_price`, the result truncates to zero. This makes all pubdata completely free in native resources for every transaction in that block, directly analogous to the AAVE `wadMul` truncation that produced fee-less loans.

---

### Finding Description

In `validation_impl.rs` line 142, `native_per_pubdata` is computed as:

```rust
let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price))
    .ok_or(TxError::Validation(InvalidTransaction::PubdataPriceTooHigh))?;
```

`wrapping_div` performs floor (truncating) integer division. When `pubdata_price < native_price` (e.g., `pubdata_price = 5`, `native_price = 10`), the result is `0`. [1](#0-0) 

By contrast, `native_per_gas` uses `div_ceil` (ceiling division), which guarantees a non-zero result for any non-zero gas price:

```rust
u256_try_to_u64(&gas_price.div_ceil(native_price))
``` [2](#0-1) 

The same floor-division pattern appears in `api/src/helpers.rs`:

```rust
let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price)).ok_or(())?;
``` [3](#0-2) 

When `native_per_pubdata == 0`, the downstream resource accounting is completely bypassed:

**1. Intrinsic pubdata overhead becomes zero** in `create_resources_for_tx`:
```rust
let intrinsic_pubdata_overhead = native_per_pubdata_byte.saturating_mul(intrinsic_pubdata);
// = 0 * intrinsic_pubdata = 0
``` [4](#0-3) 

**2. Execution pubdata charge becomes zero** in `get_resources_to_charge_for_pubdata`:
```rust
let native = current_pubdata_spent.checked_mul(native_per_pubdata)
// = current_pubdata_spent * 0 = 0
``` [5](#0-4) 

**3. The `delta_gas` adjustment for native pubdata cost is also skipped** in `compute_gas_refund`:
```rust
let delta_gas = if native_per_gas == 0 {
    0
} else {
    (native_used / native_per_gas) as i64 - (gas_used as i64)
};
```
Since `native_used` for pubdata is 0, no extra gas is charged to compensate. [6](#0-5) 

---

### Impact Explanation

When `pubdata_price < native_price`, any unprivileged user can submit transactions that generate large amounts of pubdata (e.g., many storage writes) without paying any native resource cost for it. The protocol's data availability costs are borne by the operator/protocol while the user pays only EVM gas for computation. This is a resource accounting bug: the native resource dimension of pubdata cost is silently zeroed out, breaking the economic model that ensures users pay for their share of L1 data publication costs.

---

### Likelihood Explanation

The operator sets `pubdata_price` and `native_price` as block-level metadata. A ratio where `pubdata_price < native_price` is a realistic operational state — for example, `native_price = 1000` (high proving cost) and `pubdata_price = 500` (moderate DA cost). Any user observing the current block metadata can detect this condition and exploit it. No special privileges are required beyond submitting a standard L2 transaction.

---

### Recommendation

Replace `wrapping_div` with `div_ceil` for `native_per_pubdata`, consistent with how `native_per_gas` is computed:

```rust
// Before (floor division — can produce 0):
let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price))
    .ok_or(TxError::Validation(InvalidTransaction::PubdataPriceTooHigh))?;

// After (ceiling division — consistent with native_per_gas):
let native_per_pubdata = u256_try_to_u64(&pubdata_price.div_ceil(native_price))
    .ok_or(TxError::Validation(InvalidTransaction::PubdataPriceTooHigh))?;
```

Apply the same fix in `api/src/helpers.rs`. Additionally, add a validation that rejects transactions (or blocks) where `native_per_pubdata == 0` and `pubdata_price > 0`, analogous to how the AAVE fix rejected loans with zero fees.

---

### Proof of Concept

1. Operator sets block metadata: `native_price = 1000`, `pubdata_price = 999`.
2. `native_per_pubdata = 999 / 1000 = 0` (floor division).
3. Attacker submits a transaction that writes to 100 new storage slots (generating ~3200 bytes of pubdata).
4. In `get_resources_to_charge_for_pubdata`: `native = 3200 * 0 = 0` — no native charged.
5. In `compute_gas_refund`: `native_used` for pubdata = 0, `delta_gas = 0` — no extra gas charged.
6. Attacker pays only EVM gas for the SSTORE opcodes, zero native resource cost for pubdata.
7. The operator/protocol absorbs the full L1 data availability cost for the attacker's pubdata.

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

**File:** basic_bootloader/src/bootloader/transaction_flow/gas_helpers.rs (L351-353)
```rust
    // Charge intrinsic pubdata
    let intrinsic_pubdata_overhead = native_per_pubdata_byte.saturating_mul(intrinsic_pubdata);
    let native_limit = match native_limit.checked_sub(intrinsic_pubdata_overhead) {
```

**File:** basic_bootloader/src/bootloader/transaction_flow/gas_helpers.rs (L430-432)
```rust
    let native = current_pubdata_spent
        .checked_mul(native_per_pubdata)
        .ok_or(out_of_native_resources!())?;
```

**File:** basic_bootloader/src/bootloader/transaction_flow/refund_calculation.rs (L66-80)
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
```
