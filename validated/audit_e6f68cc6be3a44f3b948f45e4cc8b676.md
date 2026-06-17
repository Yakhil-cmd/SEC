### Title
`native_per_pubdata` Truncating Division Systematically Undercharges Users for Pubdata Costs — (`basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs`)

---

### Summary

In `validation_impl.rs`, the per-pubdata-byte native cost is computed with truncating integer division (`wrapping_div`), while the per-gas native cost is computed with ceiling division (`div_ceil`). This asymmetry means every transaction that publishes pubdata pays slightly less native resource than it should, causing a systematic loss for the protocol/operator. Any unprivileged user can amplify this by publishing more pubdata (storage writes, events, contract deployments).

---

### Finding Description

During L2 transaction validation, two key ratios are derived from operator-supplied prices:

```rust
// native_per_gas uses CEILING division — rounds UP, protocol-favorable
let native_per_gas = u256_try_to_u64(&gas_price.div_ceil(native_price))...

// native_per_pubdata uses TRUNCATING division — rounds DOWN, user-favorable
let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price))...
``` [1](#0-0) 

`native_per_pubdata` is then used in two places:

1. **Intrinsic pubdata overhead** charged upfront:
   `intrinsic_pubdata_overhead = native_per_pubdata * intrinsic_pubdata`
2. **Execution pubdata charge** after execution:
   `to_charge_for_pubdata = pubdata_used * native_per_pubdata` [2](#0-1) [3](#0-2) 

When `pubdata_price % native_price != 0`, the truncation causes the user to pay:

```
actual_charge   = pubdata_used * floor(pubdata_price / native_price)
correct_charge  = pubdata_used * pubdata_price / native_price
undercharge     = pubdata_used * (pubdata_price % native_price) / native_price
```

The same truncating formula is replicated in `api/src/helpers.rs` for off-chain pre-validation: [4](#0-3) 

A secondary truncation also exists in `compute_gas_refund` where `native_used / native_per_gas` (integer division) underestimates `delta_gas` by up to 1 gas unit per transaction: [5](#0-4) 

---

### Impact Explanation

The protocol/operator receives less native resource compensation than the true cost of proving the pubdata. The undercharge per transaction is:

```
pubdata_used * (pubdata_price % native_price) / native_price
```

**Concrete example**: `pubdata_price = 1001`, `native_price = 1000`, `pubdata_used = 10,000 bytes`:
- `native_per_pubdata = 1001 / 1000 = 1` (truncated; true value is 1.001)
- User pays `10,000 × 1 = 10,000` native units
- Correct charge: `10,000 × 1.001 = 10,010` native units
- Undercharge: **10 native units** per transaction

A user publishing the maximum pubdata per block (e.g., 100,000 bytes) loses the operator ~100 native units per transaction. Multiplied across many transactions, this is a meaningful and systematic drain. The asymmetry with `native_per_gas` (which uses `div_ceil`) shows the protocol intended to protect itself from rounding but failed to apply the same logic to pubdata pricing.

---

### Likelihood Explanation

`pubdata_price % native_price != 0` is the **common case** — it holds for any pair of prices that are not exact multiples of each other. The operator sets these values dynamically based on L1 gas costs and proving costs, and there is no mechanism to enforce divisibility. Every transaction that writes to storage, emits events, or deploys contracts will trigger this undercharge. An attacker can deliberately maximize pubdata output (e.g., writing to many storage slots in a loop) to amplify the loss.

---

### Recommendation

Replace `wrapping_div` with `div_ceil` for `native_per_pubdata`, consistent with how `native_per_gas` is computed:

```rust
// Before (truncates — user-favorable):
let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price))...

// After (ceiling — protocol-favorable, consistent with native_per_gas):
let native_per_pubdata = u256_try_to_u64(&pubdata_price.div_ceil(native_price))...
```

Apply the same fix in `api/src/helpers.rs` line 427 to keep the off-chain pre-validation consistent with the bootloader.

---

### Proof of Concept

1. Operator sets `pubdata_price = 1001`, `native_price = 1000` (realistic values where `pubdata_price % native_price = 1`).
2. User submits a transaction that writes to 1,000 storage slots (each slot write generates ~66 bytes of pubdata → ~66,000 bytes total).
3. `native_per_pubdata = 1001 / 1000 = 1` (truncated).
4. User is charged `66,000 × 1 = 66,000` native units for pubdata.
5. Correct charge: `66,000 × 1.001 = 66,066` native units.
6. **Undercharge: 66 native units per transaction**, entirely due to truncation.
7. Repeating this across many transactions (or with larger pubdata) scales the loss linearly.

The user can intentionally craft transactions to maximize pubdata output (e.g., a loop that writes to fresh storage slots), making this an attacker-controlled, repeatable drain on operator revenue.

### Citations

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs (L135-143)
```rust
            u256_try_to_u64(&gas_price.div_ceil(native_price)).ok_or(TxError::Validation(
                InvalidTransaction::NativeResourcesAreTooExpensive,
            ))?
        }
    };

    // We checked native_price != 0 above
    let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price))
        .ok_or(TxError::Validation(InvalidTransaction::PubdataPriceTooHigh))?;
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
