### Title
Division-Before-Multiplication in `native_per_pubdata` Causes Systematic Pubdata Undercharging - (File: `basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs`)

---

### Summary

`native_per_pubdata` is computed with floor division (`pubdata_price / native_price`) and then multiplied by `pubdata_used` to determine the native resource cost of pubdata. This division-before-multiplication pattern causes a systematic rounding error that undercharges every transaction sender for pubdata consumption, mirroring the River.sol `_reward / totalActiveValidators` class of bug.

---

### Finding Description

In `validation_impl.rs`, the per-pubdata-byte native cost is derived as:

```rust
let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price))
``` [1](#0-0) 

`wrapping_div` on unsigned integers is floor division. This truncated scalar is then multiplied by the actual pubdata consumed in `get_resources_to_charge_for_pubdata`:

```rust
let native = current_pubdata_spent
    .checked_mul(native_per_pubdata)
``` [2](#0-1) 

The actual native charged for pubdata is therefore:

```
charged = pubdata_used × floor(pubdata_price / native_price)
```

The mathematically correct amount is:

```
correct = floor(pubdata_used × pubdata_price / native_price)
```

Because `floor(a/b) × c ≤ floor(a×c/b)` for positive integers, `charged ≤ correct` always. The rounding shortfall per transaction is:

```
shortfall = floor(pubdata_used × pubdata_price / native_price)
          − pubdata_used × floor(pubdata_price / native_price)
          ≤ pubdata_used − 1   (native units)
```

The shortfall scales linearly with `pubdata_used`. For a pubdata-heavy transaction (e.g., 100 000 bytes) with `pubdata_price mod native_price = native_price − 1`, the user avoids paying up to `pubdata_used − 1 ≈ 99 999` native units of cost.

A second, smaller instance of the same class exists in `refund_calculation.rs`:

```rust
(native_used / native_per_gas) as i64 - (gas_used as i64)
``` [3](#0-2) 

Here the truncation is bounded to at most 1 gas unit per transaction and is documented in `double_resource_accounting.md` as the intended `deltaGas` formula:

```
deltaGas := (nativeUsed / nativePerGas) - gasUsed
``` [4](#0-3) 

The primary exploitable instance is the `native_per_pubdata` one.

---

### Impact Explanation

The native resource shortfall reduces `native_used`, which in turn reduces `delta_gas` (the native-driven gas adjustment in `refund_calculation.rs`), which reduces `gas_used`, which reduces the token fee paid by the sender (`gas_used × gas_price`). Every pubdata-generating transaction is systematically undercharged. The operator/protocol receives less revenue than the pricing model intends. The error is bounded per transaction but is non-zero for any `pubdata_price` that is not an exact multiple of `native_price`, and it accumulates across all transactions in a block.

---

### Likelihood Explanation

The condition `pubdata_price mod native_price ≠ 0` is the normal operating state; the operator sets both values independently via block metadata. Any unprivileged user submitting a transaction that writes storage (the common case) triggers the code path. No special access, key, or governance action is required. The entry path is the standard L2 transaction validation flow reachable by any EOA.

---

### Recommendation

Defer the division to after the multiplication, matching the pattern recommended in the River.sol report:

```rust
// In get_resources_to_charge_for_pubdata, pass pubdata_price and native_price
// instead of the pre-divided native_per_pubdata, then compute:
let native = current_pubdata_spent
    .checked_mul(native_per_pubdata_numerator)   // pubdata_price
    .and_then(|v| Some(v / native_per_pubdata_denominator)); // native_price
```

Alternatively, keep `native_per_pubdata` but use ceiling division (`div_ceil`) so the rounding favors the protocol rather than the sender:

```rust
let native_per_pubdata = u256_try_to_u64(&pubdata_price.div_ceil(native_price))
```

Note that `native_per_gas` already uses `div_ceil` correctly:

```rust
u256_try_to_u64(&gas_price.div_ceil(native_price))
``` [5](#0-4) 

Applying the same rounding direction to `native_per_pubdata` would make the two resource dimensions consistent.

---

### Proof of Concept

**Setup:** `pubdata_price = 999`, `native_price = 1000`, `pubdata_used = 100 000`.

**Current code:**
```
native_per_pubdata = floor(999 / 1000) = 0
native_charged     = 100_000 × 0       = 0
```
The user pays **zero** native for 100 000 bytes of pubdata.

**Correct computation:**
```
correct = floor(100_000 × 999 / 1000) = floor(99_900_000 / 1000) = 99_900
```
The user should pay **99 900** native units.

**With `div_ceil` fix:**
```
native_per_pubdata = ceil(999 / 1000) = 1
native_charged     = 100_000 × 1      = 100_000
```
Slight over-approximation (100 000 vs 99 900), but it favors the protocol, consistent with how `native_per_gas` is rounded.

The attacker-controlled path: submit any EIP-1559 L2 transaction with `max_fee_per_gas` set such that `pubdata_price mod native_price` is large (close to `native_price − 1`), and include storage writes to generate pubdata. The validation path in `validate_and_compute_fee_for_transaction` → `native_per_pubdata` computation → `get_resources_to_charge_for_pubdata` is exercised on every such transaction without any privileged access. [6](#0-5) [2](#0-1) [7](#0-6)

### Citations

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs (L135-135)
```rust
            u256_try_to_u64(&gas_price.div_ceil(native_price)).ok_or(TxError::Validation(
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs (L141-143)
```rust
    // We checked native_price != 0 above
    let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price))
        .ok_or(TxError::Validation(InvalidTransaction::PubdataPriceTooHigh))?;
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

**File:** docs/double_resource_accounting.md (L48-48)
```markdown
  `deltaGas := (nativeUsed / nativePerGas) - gasUsed`
```
