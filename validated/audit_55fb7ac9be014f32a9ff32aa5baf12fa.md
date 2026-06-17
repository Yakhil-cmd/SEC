### Title
Truncating Division in `native_per_pubdata` Calculation Allows Zero-Cost Pubdata Consumption — (`basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs`)

---

### Summary

`native_per_pubdata` is computed using truncating (floor) integer division (`wrapping_div`) instead of ceiling division. This is the direct analog of the Morpho `mulDivDown` rounding bug: when `pubdata_price < native_price`, the result is **zero**, meaning the transaction pays no native resources for any pubdata it generates. Even when `pubdata_price ≥ native_price` but is not an exact multiple, users systematically underpay for pubdata. The same truncation is replicated in the public API helper.

---

### Finding Description

In `validation_impl.rs`, the per-pubdata-byte native resource rate is computed as:

```rust
// We checked native_price != 0 above
let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price))
    .ok_or(TxError::Validation(InvalidTransaction::PubdataPriceTooHigh))?;
``` [1](#0-0) 

`wrapping_div` performs truncating (floor) integer division. Compare this to `native_per_gas`, computed on the very same lines with `div_ceil`:

```rust
u256_try_to_u64(&gas_price.div_ceil(native_price))
``` [2](#0-1) 

The asymmetry is intentional for `native_per_gas` (rounding up protects the protocol), but the same protection is absent for `native_per_pubdata`.

The same truncating division is present in the public API helper:

```rust
// native_per_pubdata = pubdata_price / native_price
let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price)).ok_or(())?;
``` [3](#0-2) 

`native_per_pubdata` is subsequently used in two critical places:

1. **Intrinsic pubdata overhead** — deducted from the native limit before execution begins:

```rust
let intrinsic_pubdata_overhead = native_per_pubdata_byte.saturating_mul(intrinsic_pubdata);
let native_limit = match native_limit.checked_sub(intrinsic_pubdata_overhead) { ... };
``` [4](#0-3) 

2. **Post-execution pubdata charge** — checks whether the transaction has enough native resources to pay for all pubdata it generated:

```rust
let native = current_pubdata_spent
    .checked_mul(native_per_pubdata)
    .ok_or(out_of_native_resources!())?;
``` [5](#0-4) 

When `native_per_pubdata = 0`, both of these charges become zero. The transaction's entire native budget is available for computation, and no native resources are consumed regardless of how much pubdata is written.

---

### Impact Explanation

The ZKsync OS double-resource accounting model charges native resources for pubdata to reflect the real off-chain cost of proving and publishing data to L1. When `native_per_pubdata` is truncated to zero (i.e., `pubdata_price < native_price`), this charge is completely bypassed:

- A transaction generating thousands of storage-write pubdata bytes pays **zero** native resources for that pubdata.
- The `deltaGas` adjustment (`nativeUsed / nativePerGas - gasUsed`) is smaller than it should be, so the user is charged less gas than the actual proving/publishing cost warrants.
- The protocol bears the L1 data publication cost without collecting the corresponding fee from the user.

Even when `pubdata_price ≥ native_price` but not an exact multiple, every transaction systematically underpays by up to `(native_price - 1)` native units per pubdata byte — analogous to the Morpho `mulDivDown` case where small amounts round to zero.

---

### Likelihood Explanation

The `pubdata_price` and `native_price` are operator-set block-context values read from the oracle: [6](#0-5) 

Any block where `pubdata_price < native_price` triggers the zero-cost case. An unprivileged L2 transaction sender cannot control these values directly, but:

- The condition `pubdata_price < native_price` is a plausible operational state (e.g., during low L1 gas periods or specific operator configurations).
- Any transaction sender active during such a block can exploit the zero-cost pubdata path by maximizing storage writes (up to the block's `pubdata_limit`).
- The non-zero but truncated case (`pubdata_price % native_price != 0`) is present in virtually every block, leaking a small amount of value per transaction continuously.

---

### Recommendation

Replace `wrapping_div` with `div_ceil` for `native_per_pubdata`, consistent with how `native_per_gas` is computed:

```rust
// Before (truncates, favors user):
let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price))
    .ok_or(TxError::Validation(InvalidTransaction::PubdataPriceTooHigh))?;

// After (rounds up, favors protocol):
let native_per_pubdata = u256_try_to_u64(&pubdata_price.div_ceil(native_price))
    .ok_or(TxError::Validation(InvalidTransaction::PubdataPriceTooHigh))?;
```

Apply the same fix in `api/src/helpers.rs` line 427. [1](#0-0) [3](#0-2) 

---

### Proof of Concept

**Setup:** Operator sets `pubdata_price = 9`, `native_price = 10`.

**Computation:**
```
native_per_pubdata = 9.wrapping_div(10) = 0   // truncated to zero
```

**Effect:** A transaction that writes 1,000 storage slots (~32,000 bytes of pubdata) passes the post-execution pubdata check with zero native consumed for pubdata:

```rust
// get_resources_to_charge_for_pubdata:
let native = 32_000u64.checked_mul(0u64) = Some(0);
// resources_for_pubdata = 0 native
// has_enough(0) = true  → transaction succeeds, pubdata is free
```

The correct charge should be `ceil(9/10) * 32_000 = 1 * 32_000 = 32_000` native units. Instead, the user pays 0, and the protocol publishes 32 KB to L1 at its own expense.

### Citations

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs (L135-138)
```rust
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

**File:** basic_bootloader/src/bootloader/transaction_flow/gas_helpers.rs (L352-359)
```rust
    let intrinsic_pubdata_overhead = native_per_pubdata_byte.saturating_mul(intrinsic_pubdata);
    let native_limit = match native_limit.checked_sub(intrinsic_pubdata_overhead) {
        Some(val) => val,
        None => P::handle_arithmetic_error(
            system,
            P::native_underflow_error("subtracting pubdata overhead"),
        )?,
    };
```

**File:** basic_bootloader/src/bootloader/transaction_flow/gas_helpers.rs (L430-432)
```rust
    let native = current_pubdata_spent
        .checked_mul(native_per_pubdata)
        .ok_or(out_of_native_resources!())?;
```

**File:** zk_ee/src/system/metadata/zk_metadata.rs (L193-203)
```rust
impl ZkSpecificPricingMetadata for BlockMetadataFromOracle {
    fn get_pubdata_price(&self) -> U256 {
        self.pubdata_price
    }
    fn native_price(&self) -> U256 {
        self.native_price
    }
    fn get_pubdata_limit(&self) -> u64 {
        self.pubdata_limit
    }
}
```
