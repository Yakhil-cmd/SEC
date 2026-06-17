### Title
`native_per_pubdata` Truncates to Zero When `pubdata_price < native_price`, Allowing Free Pubdata Generation — (File: `basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs`)

---

### Summary

In ZKsync OS's L2 transaction validation, `native_per_pubdata` is computed via a truncating integer division. When the operator-set `pubdata_price` is less than `native_price`, the result truncates to zero. With `native_per_pubdata == 0`, every subsequent pubdata-charging call multiplies by zero, making all pubdata generation completely free in native resources. An unprivileged user can then submit transactions that write large amounts of state (SSTORE spam) and force the operator to publish that data to L1 without collecting any native-resource compensation.

---

### Finding Description

In `validation_impl.rs`, the per-pubdata-byte native cost is derived as:

```rust
// basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs:142
let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price))
    .ok_or(TxError::Validation(InvalidTransaction::PubdataPriceTooHigh))?;
``` [1](#0-0) 

`wrapping_div` is a truncating (floor) integer division. When `pubdata_price < native_price` (e.g., `pubdata_price = 5`, `native_price = 10`), the result is `0`. There is no guard that rejects or clamps this to a minimum of `1`.

Contrast this with `native_per_gas`, which uses `div_ceil` (rounds up):

```rust
// basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs:135
u256_try_to_u64(&gas_price.div_ceil(native_price))
``` [2](#0-1) 

The asymmetry is exact: `native_per_gas` rounds **up** (conservative, safe), while `native_per_pubdata` rounds **down** (can reach zero, unsafe).

Once `native_per_pubdata == 0`, every downstream charging site multiplies by it and produces zero cost:

**1. Intrinsic pubdata overhead in `create_resources_for_tx`:**
```rust
let intrinsic_pubdata_overhead = native_per_pubdata_byte.saturating_mul(intrinsic_pubdata);
// → 0 * intrinsic_pubdata = 0
``` [3](#0-2) 

**2. Post-execution pubdata charge in `get_resources_to_charge_for_pubdata`:**
```rust
let native = current_pubdata_spent
    .checked_mul(native_per_pubdata)  // → 0
    .ok_or(out_of_native_resources!())?;
``` [4](#0-3) 

**3. Pubdata sufficiency check always passes:**
```rust
let enough = resources.has_enough(&resources_for_pubdata); // resources_for_pubdata = 0 → always true
``` [5](#0-4) 

**4. `delta_gas` adjustment in `compute_gas_refund` never accounts for pubdata:**
Because `native_used` does not include any pubdata contribution (it was charged as 0), the `delta_gas = (native_used / native_per_gas) - gas_used` adjustment never adds extra gas for pubdata costs. [6](#0-5) 

---

### Impact Explanation

When `native_per_pubdata == 0`:

- A user can execute transactions that write to many storage slots (SSTORE), generating large L1 state diffs (pubdata), without paying any native resource cost for that pubdata.
- The operator must still publish this pubdata to L1 and pay the corresponding L1 gas cost.
- The user pays only for EVM gas (ergs), which covers computation but not the L1 data-availability cost.
- This is a direct resource accounting bypass: the operator intended to charge for pubdata (they set `pubdata_price > 0`), but the truncation silently zeroes out the charge.
- Repeated exploitation drains the operator's L1 gas budget without corresponding user payment, constituting operator financial loss.

---

### Likelihood Explanation

The condition `pubdata_price < native_price` is realistic:

- `pubdata_price` tracks L1 blob/calldata gas prices, which fluctuate and can be very low.
- `native_price` tracks proving cost (RISC-V cycles), which is relatively stable.
- During periods of low L1 gas prices (e.g., post-EIP-4844 blob fee drops), `pubdata_price` can easily fall below `native_price`.
- No on-chain enforcement prevents the operator from setting these values in this ratio; the ZKsync OS code contains no minimum check on `native_per_pubdata`.
- Once the condition holds, **any** unprivileged L2 transaction sender can exploit it by submitting SSTORE-heavy transactions.

---

### Recommendation

**Short term:** Add a guard after computing `native_per_pubdata` that rejects the transaction (or clamps to 1) if the result is zero while `pubdata_price > 0`:

```rust
let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price))
    .ok_or(TxError::Validation(InvalidTransaction::PubdataPriceTooHigh))?;

// Prevent free pubdata when pubdata_price is non-zero but rounds to zero
if native_per_pubdata == 0 && !pubdata_price.is_zero() {
    return Err(TxError::Validation(InvalidTransaction::PubdataPriceTooLow));
}
```

**Long term:** Use `div_ceil` consistently for `native_per_pubdata` (matching the approach used for `native_per_gas`) to ensure the operator always collects at least as much as intended:

```rust
let native_per_pubdata = u256_try_to_u64(&pubdata_price.div_ceil(native_price))
    .ok_or(TxError::Validation(InvalidTransaction::PubdataPriceTooHigh))?;
``` [1](#0-0) 

---

### Proof of Concept

**Setup:**
- Operator sets block context: `pubdata_price = 5`, `native_price = 10`
- `native_per_pubdata = floor(5 / 10) = 0`

**Attack:**
1. Attacker submits an L2 EIP-1559 transaction with `max_fee_per_gas = 10` (≥ base fee), `gas_limit = 1_000_000`.
2. `native_per_gas = ceil(10 / 10) = 1` → non-zero, transaction passes validation.
3. Transaction body executes 10,000 SSTORE operations writing to distinct slots → generates ~320,000 bytes of pubdata.
4. `get_resources_to_charge_for_pubdata` computes `native = 320_000 * 0 = 0`.
5. `check_enough_resources_for_pubdata` returns `enough = true`.
6. Transaction succeeds; attacker pays only EVM gas for computation.
7. Operator publishes 320,000 bytes of pubdata to L1 at their own expense.

**Concrete numbers:**
- At 320,000 bytes of pubdata and a realistic L1 blob gas price, the operator pays ~0.001 ETH per such transaction.
- An attacker can submit many such transactions per block, draining the operator's L1 gas budget. [7](#0-6) [1](#0-0)

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

**File:** basic_bootloader/src/bootloader/transaction_flow/gas_helpers.rs (L352-353)
```rust
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

**File:** basic_bootloader/src/bootloader/transaction_flow/gas_helpers.rs (L454-455)
```rust
    let enough = resources.has_enough(&resources_for_pubdata);
    Ok((enough, resources_for_pubdata, pubdata_used))
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
