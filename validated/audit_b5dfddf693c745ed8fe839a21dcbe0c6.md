### Title
Truncated Division in `native_per_pubdata` Calculation Allows Zero-Cost Pubdata Writes - (File: `basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs`)

---

### Summary

In ZKsync OS's L2 transaction validation, `native_per_pubdata` — the native resource cost per byte of pubdata — is computed using floor (truncating) integer division. When `pubdata_price < native_price`, this truncates to zero, making all pubdata completely free in terms of native resource accounting. An unprivileged transaction sender can exploit this to write large amounts of storage (generating pubdata) without paying the native resource cost that models the actual L1 publication expense.

---

### Finding Description

In `basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs` at line 142:

```rust
// We checked native_price != 0 above
let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price))
    .ok_or(TxError::Validation(InvalidTransaction::PubdataPriceTooHigh))?;
``` [1](#0-0) 

`wrapping_div` performs floor (truncating) integer division. When `pubdata_price < native_price`, the result is `0`. This is in direct contrast to how `native_per_gas` is computed on the very next lines, which correctly uses `div_ceil`:

```rust
u256_try_to_u64(&gas_price.div_ceil(native_price))
``` [2](#0-1) 

The same truncating pattern is replicated in the API helper:

```rust
let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price)).ok_or(())?;
``` [3](#0-2) 

When `native_per_pubdata == 0`, the function `get_resources_to_charge_for_pubdata` computes:

```rust
let native = current_pubdata_spent
    .checked_mul(native_per_pubdata)  // 0 * N = 0 always
    .ok_or(out_of_native_resources!())?;
``` [4](#0-3) 

This means `check_enough_resources_for_pubdata` always returns `true` (sufficient resources) regardless of how many pubdata bytes the transaction generates, and the native resource charge for pubdata is always zero.

The `delta_gas` adjustment in `compute_gas_refund` also uses floor division:

```rust
(native_used / native_per_gas) as i64 - (gas_used as i64)
``` [5](#0-4) 

But the `native_per_pubdata = 0` case is the dominant issue: pubdata native cost is entirely zeroed out, not merely rounded down by a small amount.

The condition `pubdata_price < native_price` is realistic. The existing test suite demonstrates it directly:

```rust
let native_price = U256::from(100);
let pubdata_price = U256::from(2);
``` [6](#0-5) 

In this configuration, `native_per_pubdata = 2 / 100 = 0`.

---

### Impact Explanation

The native resource models the actual off-chain cost of proving and publishing data to L1. When `native_per_pubdata` truncates to zero:

1. A transaction can perform arbitrarily many SSTORE operations (each generating 32+ bytes of pubdata) without any native resource charge for the pubdata component.
2. The protocol must still prove and publish all generated pubdata to L1, paying real ETH.
3. The transaction sender pays only EVM gas for SSTORE (which is already accounted for in ergs), but pays **zero** native resource cost for the L1 data publication burden they impose.
4. This is a direct subsidy from the protocol to the attacker: the attacker generates L1 publication costs that are not reflected in their fee payment.

At scale (many transactions, or a single transaction with many storage writes), this allows an attacker to impose unbounded L1 data costs on the protocol while paying only the EVM gas cost of the writes.

---

### Likelihood Explanation

The condition `pubdata_price < native_price` is not an edge case. It is a normal operating condition whenever the cost of one native resource unit (one RISC-V cycle equivalent) exceeds the cost of one byte of pubdata. Block parameters are set by the operator and fluctuate with L1 gas prices and proving costs. Any unprivileged user can observe the current block's `pubdata_price` and `native_price` values and submit storage-heavy transactions when the condition holds. No special access, leaked keys, or governance manipulation is required.

---

### Recommendation

Replace `wrapping_div` with `div_ceil` for the `native_per_pubdata` calculation, consistent with how `native_per_gas` is computed:

```rust
// Before (truncates to 0 when pubdata_price < native_price):
let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price))
    .ok_or(TxError::Validation(InvalidTransaction::PubdataPriceTooHigh))?;

// After (rounds up, ensuring pubdata always has non-zero cost when pubdata_price > 0):
let native_per_pubdata = u256_try_to_u64(&pubdata_price.div_ceil(native_price))
    .ok_or(TxError::Validation(InvalidTransaction::PubdataPriceTooHigh))?;
```

Apply the same fix in `api/src/helpers.rs`. This ensures the rounding error always benefits the protocol (overcharges slightly) rather than the sender (undercharges to zero), directly mirroring the Compound fix of rounding borrow balances up.

---

### Proof of Concept

Given block parameters `pubdata_price = 50`, `native_price = 100`:

1. `native_per_pubdata = 50 / 100 = 0` (floor division).
2. Attacker submits a transaction that writes to 1,000 distinct storage slots.
3. Each write generates ~64 bytes of pubdata (key + value diff), totaling ~64,000 bytes.
4. `get_resources_to_charge_for_pubdata` charges `64000 * 0 = 0` native units.
5. `check_enough_resources_for_pubdata` returns `true` unconditionally.
6. Transaction executes successfully; attacker pays only EVM gas for 1,000 SSTOREs.
7. Protocol publishes 64,000 bytes of pubdata to L1 at its own expense.

With `div_ceil`, `native_per_pubdata = ceil(50/100) = 1`, and the 64,000 bytes would cost 64,000 native units — correctly charging the sender for the L1 publication burden.

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

**File:** api/src/helpers.rs (L427-427)
```rust
    let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price)).ok_or(())?;
```

**File:** basic_bootloader/src/bootloader/transaction_flow/gas_helpers.rs (L430-432)
```rust
    let native = current_pubdata_spent
        .checked_mul(native_per_pubdata)
        .ok_or(out_of_native_resources!())?;
```

**File:** basic_bootloader/src/bootloader/transaction_flow/refund_calculation.rs (L72-72)
```rust
            (native_used / native_per_gas) as i64 - (gas_used as i64)
```

**File:** tests/instances/transactions/src/lib.rs (L1235-1236)
```rust
    let native_price = U256::from(100);
    let pubdata_price = U256::from(2);
```
