### Title
Truncating Floor Division in `native_per_pubdata` Computation Allows Pubdata Underpayment — (`basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs`)

---

### Summary

ZKsync OS computes `native_per_pubdata` — the native resource units charged per byte of pubdata — using floor (truncating) integer division (`wrapping_div`). This is inconsistent with the ceiling division (`div_ceil`) used for `native_per_gas`. When `pubdata_price < native_price`, the result is `native_per_pubdata = 0`, meaning pubdata is completely free in native resource terms. Even when `pubdata_price >= native_price`, the truncation systematically undercharges for every pubdata byte. Any unprivileged user can exploit this by submitting transactions that generate pubdata (storage writes), paying less native resource than the actual proving/DA cost.

---

### Finding Description

In `validate_and_compute_fee_for_transaction`, the two key resource ratios are computed as follows:

**`native_per_gas`** — uses ceiling division:
```rust
u256_try_to_u64(&gas_price.div_ceil(native_price))
```

**`native_per_pubdata`** — uses floor division:
```rust
let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price))
    .ok_or(TxError::Validation(InvalidTransaction::PubdataPriceTooHigh))?;
``` [1](#0-0) 

The same floor-division pattern appears in the API helper:

```rust
let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price)).ok_or(())?;
``` [2](#0-1) 

`native_per_pubdata` is then used in two critical places:

1. **Intrinsic pubdata overhead** — deducted from the native limit at transaction start:
   `intrinsic_pubdata_overhead = native_per_pubdata_byte.saturating_mul(intrinsic_pubdata)` [3](#0-2) 

2. **Runtime pubdata charging** — charged from native resources after execution:
   `native = current_pubdata_spent.checked_mul(native_per_pubdata)` [4](#0-3) 

A second, compounding source of precision loss exists in `compute_gas_refund`. The `delta_gas` adjustment — which converts excess native consumption back into gas — also uses floor division:

```rust
(native_used / native_per_gas) as i64 - (gas_used as i64)
``` [5](#0-4) 

This means even when `native_per_pubdata > 0`, the gas adjustment for native resource consumption is systematically rounded down, further reducing what the user pays.

The design intent is documented in `docs/double_resource_accounting.md`:
> `nativePerGas := gasPrice/nativePrice`
> `deltaGas := (nativeUsed / nativePerGas) - gasUsed` [6](#0-5) 

Both formulas are specified as exact ratios, but the implementation truncates both.

---

### Impact Explanation

**Case 1 — 100% error (`pubdata_price < native_price`):**
- `native_price = 1000`, `pubdata_price = 999`
- `native_per_pubdata = floor(999/1000) = 0`
- Correct value: `ceil(999/1000) = 1`
- Result: pubdata is completely free in native resource terms. A user can write to as many storage slots as EVM gas allows without any native resource cost for the resulting pubdata. The operator bears the full proving and DA cost.

**Case 2 — Systematic underpayment (general case):**
- `native_price = 1_000_000_000`, `pubdata_price = 1_999_999_999`
- `native_per_pubdata = floor(1_999_999_999 / 1_000_000_000) = 1`
- Correct value: `ceil(1_999_999_999 / 1_000_000_000) = 2`
- Error: 50% underpayment per pubdata byte
- For a transaction generating 1000 pubdata bytes: charged 1000 native units, should be charged 2000 native units

The operator/sequencer is systematically underpaid for data availability costs. Users can generate more pubdata than their fee justifies, creating a subsidy at the operator's expense. This is a direct financial loss path reachable by any unprivileged user.

---

### Likelihood Explanation

The condition `pubdata_price < native_price` is a realistic operational scenario — both values are set by the operator and can be in any ratio. The truncation error is present on every transaction that generates pubdata (any storage write), making this a consistent, not edge-case, underpayment. Any user submitting a normal EIP-1559 transaction with storage writes triggers this path. [7](#0-6) 

---

### Recommendation

Replace `wrapping_div` with `div_ceil` for `native_per_pubdata` in both locations:

```rust
// In validation_impl.rs
let native_per_pubdata = u256_try_to_u64(&pubdata_price.div_ceil(native_price))
    .ok_or(TxError::Validation(InvalidTransaction::PubdataPriceTooHigh))?;

// In api/src/helpers.rs
let native_per_pubdata = u256_try_to_u64(&pubdata_price.div_ceil(native_price)).ok_or(())?;
```

Similarly, replace the floor division in `compute_gas_refund`'s `delta_gas` with ceiling division:

```rust
(native_used.div_ceil(native_per_gas)) as i64 - (gas_used as i64)
``` [5](#0-4) 

---

### Proof of Concept

**Scenario demonstrating 100% error:**

1. Operator sets `native_price = 1000`, `pubdata_price = 500` (pubdata cheaper than native).
2. User submits an EIP-1559 transaction with `gas_price = 2000`, `gas_limit = 500_000`.
3. In `validate_and_compute_fee_for_transaction`:
   - `native_per_gas = ceil(2000 / 1000) = 2` ✓
   - `native_per_pubdata = floor(500 / 1000) = 0` ← truncated to zero
4. `native_prepaid_from_gas = 2 * 500_000 = 1_000_000` native units allocated.
5. Transaction executes, writing to 100 storage slots → ~6400 bytes of pubdata.
6. In `get_resources_to_charge_for_pubdata`: `native = 6400 * 0 = 0` native charged for pubdata.
7. In `compute_gas_refund`: `delta_gas = (native_used / 2) - gas_used` — pubdata native cost is invisible to the delta_gas adjustment since it was never charged.
8. User pays zero native resource for 6400 bytes of pubdata. Operator bears full DA cost. [8](#0-7) [4](#0-3)

### Citations

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs (L106-107)
```rust
    let pubdata_price = system.get_pubdata_price();
    let native_price = system.get_native_price();
```

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

**File:** api/src/helpers.rs (L426-427)
```rust
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

**File:** docs/double_resource_accounting.md (L38-48)
```markdown
  `nativePerGas := gasPrice/nativePrice`
Note: for call simulation we use a constant for it, as gasPrice might be set to 0.

Next we define the limit for the native resource as:
  `nativeLimit := gasLimit * nativePerGas`

Then we process the transaction, charging both Ergs for EE execution and native resource for any kind of computation (EE, bootloader or system work).

If execution doesn't run out of native resources, we first charge for pubdata from native resource.
Then we compute the difference between the implicit gas used derived from native resource consumption and the gas used by EEs from the ergs used. We call this value `deltaGas`.
  `deltaGas := (nativeUsed / nativePerGas) - gasUsed`
```
