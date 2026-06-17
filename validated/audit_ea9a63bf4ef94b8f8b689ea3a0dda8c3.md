### Title
Truncating Integer Division in `native_per_pubdata` Computation Allows Pubdata Fee Bypass — (`basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs`)

---

### Summary

`native_per_pubdata` — the native-resource cost charged per byte of pubdata — is computed using truncating (floor) integer division. When `pubdata_price < native_price`, the result rounds down to zero, making all pubdata effectively free. An attacker can exploit this to generate unlimited state-diff pubdata without paying for it, causing the operator to bear the full L1 publication cost.

---

### Finding Description

In `validate_and_compute_fee_for_transaction`, the per-pubdata-byte native cost is computed as:

```rust
// basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs, line 142
let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price))
    .ok_or(TxError::Validation(InvalidTransaction::PubdataPriceTooHigh))?;
``` [1](#0-0) 

`wrapping_div` performs truncating integer division. Whenever `pubdata_price < native_price`, the result is `0`.

By contrast, `native_per_gas` is computed with **ceiling** division:

```rust
u256_try_to_u64(&gas_price.div_ceil(native_price))
``` [2](#0-1) 

The same `native_per_pubdata = 0` value is then propagated into `create_resources_for_tx`, where the intrinsic pubdata overhead becomes:

```rust
let intrinsic_pubdata_overhead = native_per_pubdata_byte.saturating_mul(intrinsic_pubdata);
// → 0 * intrinsic_pubdata = 0
``` [3](#0-2) 

And in `get_resources_to_charge_for_pubdata`, the runtime pubdata charge is:

```rust
let native = current_pubdata_spent
    .checked_mul(native_per_pubdata)  // → 0 for any pubdata_spent
    .ok_or(out_of_native_resources!())?;
``` [4](#0-3) 

The `check_enough_resources_for_pubdata` call therefore always passes regardless of how much pubdata the transaction generates. [5](#0-4) 

The same truncating division is mirrored in the public API helper:

```rust
// api/src/helpers.rs, line 427
let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price)).ok_or(())?;
``` [6](#0-5) 

---

### Impact Explanation

When `pubdata_price < native_price` (e.g., `pubdata_price = 5`, `native_price = 10`), `native_per_pubdata` truncates to `0`. A transaction can then write to an arbitrary number of storage slots — each write producing pubdata — without consuming any native resource budget for it. The operator must still publish all state diffs to L1 and bears the full cost. This is a direct, repeatable financial loss to the operator/sequencer with no on-chain enforcement preventing it.

---

### Likelihood Explanation

The operator sets `pubdata_price` and `native_price` independently via block metadata. Any configuration where `pubdata_price < native_price` triggers the bug. The default test configuration already sets `pubdata_price = 0` and `native_price = 10`. [7](#0-6) 

In production, if the operator sets a low pubdata price relative to native price (a plausible configuration during low-congestion periods), any unprivileged L2 transaction sender can exploit this by submitting transactions that perform many storage writes at zero pubdata cost.

---

### Recommendation

Replace `wrapping_div` with `div_ceil` for `native_per_pubdata`, consistent with how `native_per_gas` is computed:

```rust
// Before (truncates to 0 when pubdata_price < native_price):
let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price))
    .ok_or(TxError::Validation(InvalidTransaction::PubdataPriceTooHigh))?;

// After (rounds up, never silently zeroes):
let native_per_pubdata = u256_try_to_u64(&pubdata_price.div_ceil(native_price))
    .ok_or(TxError::Validation(InvalidTransaction::PubdataPriceTooHigh))?;
```

Apply the same fix in `api/src/helpers.rs` line 427. Additionally, consider adding a validation that rejects transactions (or logs a critical warning) when `native_per_pubdata == 0` but `pubdata_price != 0`, to surface misconfiguration.

---

### Proof of Concept

1. Operator sets block context: `pubdata_price = 9`, `native_price = 10`.
2. `native_per_pubdata = 9 / 10 = 0` (truncating division).
3. Attacker submits a transaction that writes to 10,000 distinct storage slots (each write = ~68 bytes of pubdata → ~680,000 bytes total).
4. `get_resources_to_charge_for_pubdata` computes `680000 * 0 = 0` native cost.
5. `check_enough_resources_for_pubdata` returns `true` regardless of remaining native budget.
6. Transaction succeeds; operator publishes ~680 KB of state diffs to L1 at their own expense.
7. Attacker paid only EVM gas for the SSTORE opcodes, not for pubdata publication. [1](#0-0) [8](#0-7)

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

**File:** basic_bootloader/src/bootloader/transaction_flow/gas_helpers.rs (L351-353)
```rust
    // Charge intrinsic pubdata
    let intrinsic_pubdata_overhead = native_per_pubdata_byte.saturating_mul(intrinsic_pubdata);
    let native_limit = match native_limit.checked_sub(intrinsic_pubdata_overhead) {
```

**File:** basic_bootloader/src/bootloader/transaction_flow/gas_helpers.rs (L422-435)
```rust
pub fn get_resources_to_charge_for_pubdata<S: EthereumLikeTypes>(
    system: &mut System<S>,
    native_per_pubdata: u64,
    base_pubdata: Option<u64>,
) -> Result<(u64, S::Resources), SystemError> {
    let current_pubdata_spent = system
        .net_pubdata_used()?
        .saturating_sub(base_pubdata.unwrap_or(0));
    let native = current_pubdata_spent
        .checked_mul(native_per_pubdata)
        .ok_or(out_of_native_resources!())?;
    let native = <S::Resources as zk_ee::system::Resources>::Native::from_computational(native);
    Ok((current_pubdata_spent, S::Resources::from_native(native)))
}
```

**File:** basic_bootloader/src/bootloader/transaction_flow/gas_helpers.rs (L444-456)
```rust
///
pub fn check_enough_resources_for_pubdata<S: EthereumLikeTypes>(
    system: &mut System<S>,
    native_per_pubdata: u64,
    resources: &S::Resources,
    base_pubdata: Option<u64>,
) -> Result<(bool, S::Resources, u64), SystemError> {
    let (pubdata_used, resources_for_pubdata) =
        get_resources_to_charge_for_pubdata(system, native_per_pubdata, base_pubdata)?;
    system_log!(system, "Checking gas for pubdata, resources_for_pubdata: {resources_for_pubdata:?}, resources: {resources:?}\n");
    let enough = resources.has_enough(&resources_for_pubdata);
    Ok((enough, resources_for_pubdata, pubdata_used))
}
```

**File:** api/src/helpers.rs (L426-428)
```rust
    // native_per_pubdata = pubdata_price / native_price
    let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price)).ok_or(())?;

```

**File:** zk_ee/src/system/metadata/zk_metadata.rs (L208-211)
```rust
            eip1559_basefee: U256::from(1000u64),
            pubdata_price: U256::from(0u64),
            native_price: U256::from(10),
            block_number: 1,
```
