### Title
Floor Division in `native_per_pubdata` Calculation Allows Users to Systematically Underpay for Pubdata - (File: `basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs`)

### Summary

`native_per_pubdata` — the native-resource cost charged per byte of pubdata — is computed with floor (truncating) division instead of ceiling division. Because this value is used to *charge* users for pubdata consumption, it should round up to ensure users pay at least the correct amount. The floor division causes a systematic undercharge that propagates through the `delta_gas` adjustment and results in users paying less ETH than they should for every transaction that writes pubdata.

### Finding Description

In `validate_and_compute_fee_for_transaction`, the per-pubdata native cost is computed as:

```rust
// basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs, line 142
let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price))
    .ok_or(TxError::Validation(InvalidTransaction::PubdataPriceTooHigh))?;
``` [1](#0-0) 

`wrapping_div` performs integer floor division, truncating any remainder. Contrast this with `native_per_gas`, computed on the very next lines, which explicitly uses ceiling division:

```rust
// line 135
u256_try_to_u64(&gas_price.div_ceil(native_price))
``` [2](#0-1) 

The same asymmetry is present in the public API helper `validate_l2_tx_intrinsic_native_resources`, where the comment even makes the inconsistency explicit:

```
// native_per_gas = ceil(gas_price / native_price)   ← div_ceil used
// native_per_pubdata = pubdata_price / native_price  ← wrapping_div (floor) used
``` [3](#0-2) 

`native_per_pubdata` is used in two places that directly affect how much the user pays:

1. **Intrinsic pubdata overhead** — deducted from the native limit before execution:
   `intrinsic_pubdata_overhead = native_per_pubdata.saturating_mul(intrinsic_pubdata)` [4](#0-3) 

2. **Execution pubdata charge** — charged after execution in `get_resources_to_charge_for_pubdata`:
   `native = current_pubdata_spent.checked_mul(native_per_pubdata)` [5](#0-4) 

Both charges are too low when `pubdata_price` is not an exact multiple of `native_price`.

The undercharged `native_used` then flows into the `delta_gas` adjustment in `compute_gas_refund`:

```rust
// refund_calculation.rs, line 72
(native_used / native_per_gas) as i64 - (gas_used as i64)
``` [6](#0-5) 

A lower `native_used` → lower `delta_gas` → lower `gas_used` → larger refund → user pays less ETH. The documentation confirms this chain:

> `deltaGas := (nativeUsed / nativePerGas) - gasUsed` … If `deltaGas > 0`, we add it to `gasUsed` and charge it from ergs. [7](#0-6) 

### Impact Explanation

For every byte of pubdata, `native_per_pubdata` is underestimated by up to 1 native unit (the floor vs. ceil difference). For a transaction writing `N` bytes of pubdata, `native_used` is underestimated by up to `N` native units. This reduces `delta_gas` by up to `N / native_per_gas` gas units, and the ETH undercharge per byte is approximately `native_price` wei. For a transaction with thousands of pubdata bytes and a non-trivial `native_price`, the undercharge is measurable and systematic. Users can exploit this to publish pubdata at a slightly lower cost than the protocol intends, effectively subsidizing their pubdata at the protocol's expense.

### Likelihood Explanation

Every L2 transaction that writes storage (i.e., generates pubdata) is affected. No special privileges are required — any unprivileged user submitting a standard EIP-1559 or ZK transaction triggers this path. The condition `pubdata_price % native_price != 0` (which makes floor ≠ ceil) is the normal operating state whenever the operator sets prices that are not exact multiples of each other.

### Recommendation

Replace `wrapping_div` with `div_ceil` for the `native_per_pubdata` calculation, consistent with how `native_per_gas` is computed:

```rust
// Before (line 142, validation_impl.rs):
let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price))

// After:
let native_per_pubdata = u256_try_to_u64(&pubdata_price.div_ceil(native_price))
```

Apply the same fix to `api/src/helpers.rs` line 427.

### Proof of Concept

Let:
- `pubdata_price = 101` (native units per pubdata byte)
- `native_price = 10`
- `native_per_pubdata` (floor) = `101 / 10 = 10`
- `native_per_pubdata` (ceil) = `ceil(101 / 10) = 11`

A transaction writing 1000 bytes of pubdata:
- Correct native charge: `1000 * 11 = 11_000`
- Actual native charge: `1000 * 10 = 10_000`
- Undercharge: `1000` native units

With `native_per_gas = ceil(gas_price / native_price)`, say `native_per_gas = 5`:
- `delta_gas` underestimated by `1000 / 5 = 200` gas units
- ETH undercharge: `200 * gas_price` wei per transaction

Any unprivileged user can trigger this by submitting any transaction that writes to storage, which is the common case for all meaningful DeFi interactions.

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

**File:** api/src/helpers.rs (L420-427)
```rust
    // native_per_gas = ceil(gas_price / native_price)
    if native_price.is_zero() {
        return Err(());
    }
    let native_per_gas = u256_try_to_u64(&gas_price.div_ceil(native_price)).ok_or(())?;

    // native_per_pubdata = pubdata_price / native_price
    let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price)).ok_or(())?;
```

**File:** basic_bootloader/src/bootloader/transaction_flow/gas_helpers.rs (L352-353)
```rust
    let intrinsic_pubdata_overhead = native_per_pubdata_byte.saturating_mul(intrinsic_pubdata);
    let native_limit = match native_limit.checked_sub(intrinsic_pubdata_overhead) {
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

**File:** docs/double_resource_accounting.md (L47-50)
```markdown
Then we compute the difference between the implicit gas used derived from native resource consumption and the gas used by EEs from the ergs used. We call this value `deltaGas`.
  `deltaGas := (nativeUsed / nativePerGas) - gasUsed`

If `deltaGas > 0`, we add it to `gasUsed` and charge it from ergs. This ensures that gas estimation will include additional gas to cover for native resources using just base fee. We expect the base fee to be enough to cover most transactions without the need of additional gas.
```
