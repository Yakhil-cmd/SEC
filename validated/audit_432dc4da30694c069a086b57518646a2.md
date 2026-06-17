### Title
Floor Division in `native_per_pubdata` Calculation Causes Systematic Pubdata Undercharging — (`basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs`)

---

### Summary

The `native_per_pubdata` value, which controls how many native resources a user is charged per byte of pubdata generated, is computed using floor (truncating) integer division. This is inconsistent with `native_per_gas`, which correctly uses ceiling division. When `pubdata_price < native_price`, the result truncates to zero, making pubdata entirely free in native resources. Even when the result is non-zero, users are systematically undercharged for pubdata.

---

### Finding Description

In `validate_and_compute_fee_for_transaction` (the L2 transaction validation path), two resource-rate values are derived from block-level pricing parameters:

```rust
// native_per_gas uses ceiling division — correct, rounds UP to protect protocol
let native_per_gas = u256_try_to_u64(&gas_price.div_ceil(native_price))...;

// native_per_pubdata uses floor division — incorrect, rounds DOWN, undercharges users
let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price))
    .ok_or(TxError::Validation(InvalidTransaction::PubdataPriceTooHigh))?;
```

`native_per_pubdata` is then used in two critical places:

1. **`create_resources_for_tx`** — to compute `intrinsic_pubdata_overhead = native_per_pubdata_byte.saturating_mul(intrinsic_pubdata)`, which is subtracted from the user's native budget before execution.
2. **`get_resources_to_charge_for_pubdata`** — to compute `native = current_pubdata_spent.checked_mul(native_per_pubdata)`, the native cost charged for all pubdata generated during execution.

When `pubdata_price < native_price` (e.g., `pubdata_price = 5`, `native_price = 10`), `wrapping_div` yields `0`. Both charges become zero, meaning the user pays **no native resources** for any amount of pubdata generated. Even when `pubdata_price` is a non-zero multiple-minus-one of `native_price` (e.g., `pubdata_price = 19`, `native_price = 10`), the result is `1` instead of the correct ceiling value of `2`, systematically undercharging by up to ~50%.

The same floor-division pattern is replicated in the public API helper `validate_l2_tx_intrinsic_native_resources`:

```rust
// api/src/helpers.rs:427
let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price)).ok_or(())?;
```

---

### Impact Explanation

- **Zero-cost pubdata:** When `pubdata_price < native_price`, any unprivileged transaction sender can generate arbitrary amounts of pubdata (storage writes, logs, etc.) without consuming any native resources. The operator bears the L1 data availability cost without compensation through the native resource mechanism.
- **Systematic undercharging:** Even when `native_per_pubdata > 0`, users are charged less than the true cost of pubdata, since the floor division discards the fractional part. The operator is consistently undercompensated.
- **Inconsistency with `native_per_gas`:** The `native_per_gas` path uses `div_ceil` to round up, explicitly protecting the protocol. The `native_per_pubdata` path uses `wrapping_div` (floor), creating an asymmetric and exploitable inconsistency.

**Impact: 5/10** — Operator revenue loss and potential pubdata spam; does not directly drain user funds but undermines the economic model for L1 data costs.

---

### Likelihood Explanation

The `pubdata_price` and `native_price` are block-level parameters. In any configuration where `pubdata_price` is not an exact integer multiple of `native_price` — which is the common case in practice — truncation occurs. The zero-charge case (`pubdata_price < native_price`) is reachable under normal operator configurations. No privileged access is required by the attacker; any transaction sender benefits automatically.

**Likelihood: 4/10** — Depends on the ratio of operator-set block parameters, but the truncation is always present and the zero-charge case is a realistic configuration.

---

### Recommendation

Replace `wrapping_div` with `div_ceil` for the `native_per_pubdata` calculation, consistent with how `native_per_gas` is computed:

```rust
// Before (floor division — undercharges):
let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price))
    .ok_or(TxError::Validation(InvalidTransaction::PubdataPriceTooHigh))?;

// After (ceiling division — consistent with native_per_gas):
let native_per_pubdata = u256_try_to_u64(&pubdata_price.div_ceil(native_price))
    .ok_or(TxError::Validation(InvalidTransaction::PubdataPriceTooHigh))?;
```

Apply the same fix in `api/src/helpers.rs:427`.

---

### Proof of Concept

**Setup:** Block context with `native_price = 10`, `pubdata_price = 9` (a realistic ratio where pubdata is cheaper than native).

**Step 1:** `native_per_pubdata = 9.wrapping_div(10) = 0` (floor division truncates to zero).

**Step 2:** In `create_resources_for_tx`, `intrinsic_pubdata_overhead = 0 * intrinsic_pubdata = 0`. No native is reserved for intrinsic pubdata.

**Step 3:** In `get_resources_to_charge_for_pubdata`, `native = pubdata_bytes_generated * 0 = 0`. No native is charged for execution pubdata regardless of how many storage slots are written.

**Step 4:** The user's transaction writes to 100 storage slots (generating ~3200 bytes of pubdata). Native resources consumed for pubdata: `0`. The operator pays L1 DA costs for 3200 bytes but receives zero native compensation for it.

**Correct behavior:** With `div_ceil`, `native_per_pubdata = ceil(9/10) = 1`, and the user would be charged `3200 * 1 = 3200` native units for the pubdata. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

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

**File:** basic_bootloader/src/bootloader/transaction_flow/gas_helpers.rs (L430-432)
```rust
    let native = current_pubdata_spent
        .checked_mul(native_per_pubdata)
        .ok_or(out_of_native_resources!())?;
```

**File:** basic_bootloader/src/bootloader/transaction_flow/refund_calculation.rs (L69-73)
```rust
        let delta_gas = if native_per_gas == 0 {
            0
        } else {
            (native_used / native_per_gas) as i64 - (gas_used as i64)
        };
```
