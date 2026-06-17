### Title
`native_per_pubdata` Truncates to Zero via Floor Division When `pubdata_price < native_price`, Allowing Free Pubdata Generation - (File: `basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs`)

---

### Summary

When the operator-set `pubdata_price` is less than `native_price`, the floor division used to compute `native_per_pubdata` silently produces zero. This zeroes out all pubdata-related native resource charges for every transaction in the block, allowing any transaction sender to generate unlimited L1 pubdata without paying for it.

---

### Finding Description

In `validate_and_compute_fee_for_transaction`, the per-pubdata-byte native cost is computed as:

```rust
// basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs:142
let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price))
    .ok_or(TxError::Validation(InvalidTransaction::PubdataPriceTooHigh))?;
```

`wrapping_div` is integer floor division. When `pubdata_price < native_price` (e.g., `pubdata_price = 9`, `native_price = 10`), the result is `0`. There is no guard that rejects or corrects a zero `native_per_pubdata`.

This zero propagates through the entire resource accounting pipeline:

**1. Intrinsic pubdata overhead is zeroed in `create_resources_for_tx`:**
```rust
// basic_bootloader/src/bootloader/transaction_flow/gas_helpers.rs:352
let intrinsic_pubdata_overhead = native_per_pubdata_byte.saturating_mul(intrinsic_pubdata);
// = 0 * intrinsic_pubdata = 0
```

**2. Execution pubdata charge is zeroed in `get_resources_to_charge_for_pubdata`:**
```rust
// basic_bootloader/src/bootloader/transaction_flow/gas_helpers.rs:430-432
let native = current_pubdata_spent
    .checked_mul(native_per_pubdata)  // = pubdata_bytes * 0 = 0
    .ok_or(out_of_native_resources!())?;
```

**3. The pubdata sufficiency check always passes in `check_enough_resources_for_pubdata`:**
```rust
// basic_bootloader/src/bootloader/transaction_flow/gas_helpers.rs:454
let enough = resources.has_enough(&resources_for_pubdata); // resources_for_pubdata = 0, always true
```

The same floor-division pattern is replicated in `api/src/helpers.rs:427` for the off-chain intrinsic validation helper, confirming this is a systemic pattern.

The `delta_gas` adjustment in `compute_gas_refund` also uses floor division:
```rust
// basic_bootloader/src/bootloader/transaction_flow/refund_calculation.rs:72
(native_used / native_per_gas) as i64 - (gas_used as i64)
```
When `native_per_pubdata = 0`, no native is consumed for pubdata at all, so `native_used` reflects only computational native, and the delta_gas adjustment does not compensate for the missing pubdata cost.

---

### Impact Explanation

When `pubdata_price < native_price`, every L2 transaction in the block pays **zero native resources for pubdata**, regardless of how many storage slots it writes. The operator must still publish all state diffs to L1 (paying L1 gas), but receives no compensation from transaction senders. An attacker can craft transactions that write many storage slots (maximizing pubdata) at no extra cost, forcing the operator to absorb unbounded L1 data costs. This is a direct protocol-level funds-loss path for the operator.

---

### Likelihood Explanation

The condition `pubdata_price < native_price` is reachable in normal operation. The `native_price` reflects proving cost (a constant or slowly-changing value), while `pubdata_price` reflects L1 calldata/blob cost (which fluctuates with L1 gas prices). During periods of low L1 activity, `pubdata_price` can legitimately fall below `native_price`. The operator may not be aware that this causes `native_per_pubdata = 0` rather than a small positive value. Any unprivileged L2 transaction sender can exploit this condition whenever it holds.

---

### Recommendation

Replace the floor division with ceiling division for `native_per_pubdata`, consistent with how `native_per_gas` is computed:

```rust
// Current (floor division - can produce 0):
let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price))
    .ok_or(TxError::Validation(InvalidTransaction::PubdataPriceTooHigh))?;

// Fix (ceiling division - consistent with native_per_gas):
let native_per_pubdata = u256_try_to_u64(&pubdata_price.div_ceil(native_price))
    .ok_or(TxError::Validation(InvalidTransaction::PubdataPriceTooHigh))?;
```

Alternatively, add an explicit check that rejects transactions (or treats pubdata as free only when `pubdata_price == 0`) when `native_per_pubdata` would be zero but `pubdata_price > 0`:

```rust
if native_per_pubdata == 0 && !pubdata_price.is_zero() {
    native_per_pubdata = 1; // minimum charge
}
```

The same fix should be applied in `api/src/helpers.rs:427`.

---

### Proof of Concept

Set block context with `native_price = 10`, `pubdata_price = 9` (a realistic scenario during low L1 gas). Submit a transaction that writes 20 storage slots (generating ~1300 bytes of pubdata). With the bug, `native_per_pubdata = 9/10 = 0`, so the transaction pays zero native for pubdata. Without the bug (using `div_ceil`), `native_per_pubdata = 1`, and the transaction is charged `1300 * 1 = 1300` native units for pubdata.

Concretely, using the existing test infrastructure:

```rust
let block_context = BlockContext {
    native_price: U256::from(10),
    pubdata_price: U256::from(9), // pubdata_price < native_price → native_per_pubdata = 0
    eip1559_basefee: U256::from(100),
    ..Default::default()
};
// Deploy a contract that writes 20 storage slots, submit tx.
// Observe: tx succeeds and gas_used does NOT include any pubdata cost,
// even though ~1300 bytes of pubdata were generated.
// With div_ceil fix: native_per_pubdata = 1, pubdata cost = 1300 native units,
// which would be reflected in gas_used via delta_gas.
``` [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs (L141-143)
```rust
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

**File:** basic_bootloader/src/bootloader/transaction_flow/gas_helpers.rs (L429-434)
```rust
        .saturating_sub(base_pubdata.unwrap_or(0));
    let native = current_pubdata_spent
        .checked_mul(native_per_pubdata)
        .ok_or(out_of_native_resources!())?;
    let native = <S::Resources as zk_ee::system::Resources>::Native::from_computational(native);
    Ok((current_pubdata_spent, S::Resources::from_native(native)))
```

**File:** basic_bootloader/src/bootloader/transaction_flow/gas_helpers.rs (L450-455)
```rust
) -> Result<(bool, S::Resources, u64), SystemError> {
    let (pubdata_used, resources_for_pubdata) =
        get_resources_to_charge_for_pubdata(system, native_per_pubdata, base_pubdata)?;
    system_log!(system, "Checking gas for pubdata, resources_for_pubdata: {resources_for_pubdata:?}, resources: {resources:?}\n");
    let enough = resources.has_enough(&resources_for_pubdata);
    Ok((enough, resources_for_pubdata, pubdata_used))
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

**File:** api/src/helpers.rs (L426-427)
```rust
    // native_per_pubdata = pubdata_price / native_price
    let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price)).ok_or(())?;
```
